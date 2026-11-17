#!/usr/bin/env bash
# Phase driver for the sqlite-app workload. See README.md.
#
#   run.sh <new-data-dir>                    full lab profile (about 25 minutes)
#   PROFILE=smoke run.sh <new-data-dir>      tiny dataset, seconds-long phases
#
# Prints "<UTC> <phase> start" marks and a final "<UTC> done" for
# tools/lab_windows.py. All other output is JSON lines from app.py (starting
# with "{") or plain environment text, neither of which matches a mark.
# Preparation is not marked. Any failing step exits non-zero before "done".
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 <new-data-dir>" >&2; exit 2; }
dir=$1
here=$(cd "$(dirname "$0")" && pwd)
app=(python3 "$here/app.py")

profile=${PROFILE:-full}
case "$profile" in
  full)
    : "${PRESET:=medium}" "${IDLE_S:=90}" "${WARMUP_S:=60}" "${PHASE_S:=90}" "${IMPORT_S:=60}" "${SETTLE_S:=20}"
    : "${SEARCH_RATES:=1000 5000 max}" "${REPORT_RATES:=1 3 max}" "${UPDATE_RATES:=50 200 max}"
    : "${IMPORT_BATCHES:=1 100}" ;;
  smoke)
    : "${PRESET:=tiny}" "${IDLE_S:=2}" "${WARMUP_S:=2}" "${PHASE_S:=2}" "${IMPORT_S:=2}" "${SETTLE_S:=1}"
    : "${SEARCH_RATES:=500 max}" "${REPORT_RATES:=5 max}" "${UPDATE_RATES:=50 max}"
    : "${IMPORT_BATCHES:=1 100}" ;;
  *) echo "PROFILE must be full or smoke, got $profile" >&2; exit 2 ;;
esac
SEED=${SEED:-42}
# Caps bound database growth when a phase runs unrestricted (about 2 KB per
# update op on the medium preset); a phase that hits one ends early and logs it.
UPDATE_MAX_OPS=${UPDATE_MAX_OPS:-300000}
IMPORT_MAX_ROWS=${IMPORT_MAX_ROWS:-3000000}
REQUIRE_DISK_FS=${REQUIRE_DISK_FS:-0}

mark() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
rate_args() { if [[ $1 == max ]]; then :; else echo "--rate $1"; fi; }

if [[ -e "$dir" ]] && [[ -n "$(ls -A -- "$dir")" ]]; then
  echo "refusing to use non-empty $dir; give a new directory" >&2
  exit 1
fi
mkdir -p -- "$dir"
base=$dir/base.db
work=$dir/work.db

echo "sqlite-app run: profile=$profile preset=$PRESET seed=$SEED dir=$dir"
echo "phases: idle=${IDLE_S}s warmup=${WARMUP_S}s phase=${PHASE_S}s import=${IMPORT_S}s settle=${SETTLE_S}s"
echo "rates: search=[$SEARCH_RATES] report=[$REPORT_RATES] update=[$UPDATE_RATES] import batches=[$IMPORT_BATCHES]"
echo "caps: update_max_ops=$UPDATE_MAX_OPS import_max_rows=$IMPORT_MAX_ROWS"
echo "host: $(uname -srm) nproc=$(nproc) python=$(python3 -c 'import platform;print(platform.python_version())')"
echo "storage backing $dir:"
findmnt -T "$dir" -o TARGET,SOURCE,FSTYPE,OPTIONS || true
df -B1 -- "$dir" || true
fstype=$(findmnt -n -T "$dir" -o FSTYPE 2>/dev/null || echo unknown)
if [[ $REQUIRE_DISK_FS == 1 ]]; then
  case "$fstype" in
    ext4|xfs) ;;
    *) echo "Expected disk-backed ext4/XFS for $dir, got $fstype; check the SUT storage." >&2; exit 1 ;;
  esac
fi

# Unmeasured: generate, load, index and verify the dataset.
"${app[@]}" prepare --dir "$dir" --preset "$PRESET" --seed "$SEED"

# Work copies are files this script creates itself; it removes only them.
drop_work() { rm -f -- "$work" "$work-wal" "$work-shm"; }
restore() {
  mark "restore start"
  drop_work
  "${app[@]}" clone --src "$base" --dst "$work"
  sleep "$SETTLE_S"
}
verify_work() {
  mark "verify start"
  "${app[@]}" verify --db "$work"
  drop_work
}

mark "idle start"; sleep "$IDLE_S"

# Warm-up exercises the measured read paths; it does not guarantee full cache
# residency. Its own mark keeps it out of the measured phases.
mark "warmup start"
half=$(python3 -c "print($WARMUP_S / 2)")
"${app[@]}" search --db "$base" --seed 1000 --duration "$half" --label warmup
"${app[@]}" report --db "$base" --seed 1001 --duration "$half" --label warmup

for r in $SEARCH_RATES; do
  mark "search rate=$r start"
  # shellcheck disable=SC2046
  "${app[@]}" search --db "$base" --seed "$SEED" --duration "$PHASE_S" $(rate_args "$r") --label "search rate=$r"
done
for r in $REPORT_RATES; do
  mark "report rate=$r start"
  # shellcheck disable=SC2046
  "${app[@]}" report --db "$base" --seed "$SEED" --duration "$PHASE_S" $(rate_args "$r") --label "report rate=$r"
done
# Each write phase starts from an identical copy of base.db and is verified afterwards.
for r in $UPDATE_RATES; do
  restore
  mark "update rate=$r start"
  # shellcheck disable=SC2046
  "${app[@]}" update --db "$work" --seed "$SEED" --duration "$PHASE_S" --max-ops "$UPDATE_MAX_OPS" \
    $(rate_args "$r") --label "update rate=$r"
  verify_work
done
for b in $IMPORT_BATCHES; do
  restore
  mark "import batch=$b start"
  "${app[@]}" import --db "$work" --csv "$dir/receipts.csv" --batch "$b" --seed "$SEED" \
    --duration "$IMPORT_S" --max-ops $(( (IMPORT_MAX_ROWS + b - 1) / b )) --label "import batch=$b"
  verify_work
done

mark "idle start"; sleep "$IDLE_S"
mark "done"
