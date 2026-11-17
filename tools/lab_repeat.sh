#!/usr/bin/env bash
# Run workload Jobs back to back for several rounds, on a lab cp node.
#
#   scp -r workloads tools/lab_repeat.sh lab:prac-sus/
#   ssh cp-0x 'nohup setsid prac-sus/lab_repeat.sh [--perf] 4 prac-sus/workloads/cpu-bound.yaml \
#       prac-sus/workloads/memory-bound.yaml >/dev/null 2>&1 &'
#
# Runs on the cp node so it survives our own network drops. Everything goes
# to ~/lab-runs/<UTC start>/, which is the ZFS home and outlives the booking:
#   runs.log            one line per Job start/end, with UTC times
#   r<N>-<job>.log      the Job's own log (its step timestamps)
#   prometheus.tar.gz   TSDB snapshot of the in-cluster Prometheus, taken
#                       after the last round (it is wiped when the slot ends)
#
# --perf turns on node-exporter's perf collector on the SUT first, so the
# snapshot also holds hardware counters (node_perf_cache_misses_total and
# others, per CPU). It needs root with all capabilities: the SUTs have
# perf_event_paranoid=3, and Kubernetes gives a non-root container no
# effective capabilities.
set -uo pipefail

perf=0
[[ ${1:-} == --perf ]] && { perf=1; shift; }
[[ $# -ge 2 ]] || { echo "usage: $0 [--perf] <rounds> <job.yaml>..." >&2; exit 2; }
rounds=$1; shift
out=~/lab-runs/$(date -u +%Y%m%d-%H%M); mkdir -p "$out"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$out/runs.log"; }

log "start rounds=$rounds jobs=$* node=$(hostname) perf=$perf"
if (( perf )); then
  ds=kube-prometheus-stack-prometheus-node-exporter
  if kubectl -n monitoring get ds "$ds" -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- --collector.perf; then
    log "perf collector already on"
  else
    cpus=$(kubectl get node -l role=sut -o jsonpath='{.items[0].status.capacity.cpu}')
    kubectl -n monitoring patch ds "$ds" --type=json -p "[
      {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"--collector.perf\"},
      {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"--collector.perf.cpus=0-$((cpus - 1))\"},
      {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/securityContext\",\"value\":{\"privileged\":true,\"runAsUser\":0,\"runAsNonRoot\":false,\"readOnlyRootFilesystem\":true}}
    ]" >/dev/null
    kubectl -n monitoring rollout status ds/"$ds" --timeout=180s >/dev/null && log "perf collector on (cpus 0-$((cpus - 1)))" || log "perf collector FAILED"
  fi
  sleep 30  # let Prometheus scrape the new series before the first job
fi
for r in $(seq 1 "$rounds"); do
  for m in "$@"; do
    name=$(kubectl create --dry-run=client -o jsonpath='{.metadata.name}' -f "$m")
    kubectl delete job "$name" --ignore-not-found --wait >/dev/null
    log "round=$r job=$name apply"
    kubectl apply -f "$m" >/dev/null
    if kubectl wait --for=condition=complete "job/$name" --timeout=40m >/dev/null; then
      log "round=$r job=$name complete"
    else
      log "round=$r job=$name FAILED or timed out"
    fi
    kubectl logs "job/$name" >"$out/r$r-$name.log" 2>&1
    sleep 60  # let the SUT settle back to idle between Jobs
  done
done

# Same steps as tools/capture.sh, without a kube context.
pod=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}')
ex() { kubectl -n monitoring exec "$pod" -c prometheus -- "$@"; }
snap=$(ex wget -qO- --post-data='' http://localhost:9090/api/v1/admin/tsdb/snapshot | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')
if [[ -n "$snap" ]] && ex tar czf - -C /prometheus/snapshots "$snap" >"$out/prometheus.tar.gz"; then
  ex rm -rf "/prometheus/snapshots/$snap"
  log "captured in-cluster Prometheus snapshot $snap"
else
  rm -f "$out/prometheus.tar.gz"
  log "Prometheus snapshot FAILED"
fi
log "done"
