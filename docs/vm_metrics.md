# Metrics: training snapshots vs. Kubernetes in a VM

Which Prometheus metrics our power model can rely on at runtime in a
virtualized Kubernetes cluster, and which exist only in the bare-metal training
snapshots.

**Rule of thumb: only use features from the "available in both" set.** Anything
else is a feature the model will not have at runtime.

## Setup compared

| | Training snapshots | VM cluster |
|---|---|---|
| Source | `snapshots/stress-1008`, `snapshots/teastore-1008`, node `sut-02` | [power-kind](https://github.com/Information-System-Engineering/power-kind) on a KVM guest, node `power-kind-worker` |
| Hardware | Supermicro X11SPi-TF, Xeon Silver 4114, 10 cores / 20 threads | Hetzner vServer (KVM/QEMU), AMD EPYC-Rome, 8 vCPUs |
| Kubernetes | v1.34.3, containerd 2.2.1 | v1.37.0, containerd 2.3.4 (kind) |
| Monitoring | kube-prometheus-stack, node-exporter, cAdvisor, process-exporter | same stack, power-kind values |

kind runs its nodes as containers on the VM, so node-exporter reads the VM
kernel's `/proc` and `/sys`. That makes the node-level signals the same as on a
Kubernetes node that is itself a VM.

Measured 2026-11-03. Only metric families that describe the machine or its
workloads are compared (`node_*`, `container_*`, `machine_*`,
`namedprocess_*`). Kubernetes control-plane and exporter self-metrics (`kube_*`,
`apiserver_*`, `go_*`, `prometheus_*`, ...) are not power signals and are left
out.

| | Count |
|---|---|
| Available in both | 333 |
| Training only | 68 |
| VM only | 16 |

## Training only: do not use as features

### Missing because of virtualization

The guest has no access to these hardware interfaces, so they will be absent in
any VM.

| Metric | Why it is missing |
|---|---|
| `node_cpu_scaling_frequency_hertz`, `node_cpu_scaling_frequency_min_hertz`, `node_cpu_scaling_frequency_max_hertz`, `node_cpu_frequency_min_hertz`, `node_cpu_frequency_max_hertz`, `node_cpu_scaling_governor` | no cpufreq driver in the guest |
| `node_cpu_core_throttles_total`, `node_cpu_package_throttles_total` | no thermal throttle MSRs |
| `node_hwmon_chip_names`, `node_hwmon_sensor_label`, `node_hwmon_temp_celsius`, `node_hwmon_temp_alarm`, `node_hwmon_temp_crit_alarm_celsius`, `node_hwmon_temp_crit_celsius`, `node_hwmon_temp_max_celsius`, `node_hwmon_temp_min_celsius`, `node_hwmon_power_average_interval_seconds`, `node_hwmon_power_average_interval_min_seconds`, `node_hwmon_power_average_interval_max_seconds`, `node_hwmon_power_is_battery_watt` | no hardware sensors passed through |
| `node_thermal_zone_temp` | no thermal zones (cooling devices exist, see below) |
| `node_edac_correctable_errors_total`, `node_edac_uncorrectable_errors_total`, `node_edac_csrow_correctable_errors_total`, `node_edac_csrow_uncorrectable_errors_total` | no memory controller |
| `machine_dimm_count`, `machine_dimm_capacity_bytes` | virtual RAM has no DIMMs |
| `node_nvme_info`, `node_disk_ata_rotation_rate_rpm`, `node_disk_ata_write_cache`, `node_disk_ata_write_cache_enabled`, `node_disk_device_mapper_info`, `node_disk_filesystem_info` | virtual disks |
| `node_infiniband_info`, `node_infiniband_physical_state_id`, `node_infiniband_rate_bytes_per_second`, `node_infiniband_state_id` | no InfiniBand NIC |

`python/powermodel.py` uses only CPU execution counters: user/nice and
system/irq/softirq. It no longer needs frequency, so missing cpufreq metrics
do not prevent VM predictions.

### Missing because the clusters are configured differently

These would probably be available in a VM if the cluster were set up like the
TUM one. They are not reliable either way.

| Metric | Cause |
|---|---|
| `node_interrupts_total` | node-exporter `interrupts` collector, off by default |
| `node_ipvs_*` (8 metrics), `node_nf_conntrack_stat_*` (8 metrics) | kube-proxy in IPVS mode on the TUM cluster, iptables in kind |
| `container_cpu_load_d_average_10s` | cAdvisor setting |
| `node_collector_*`, `node_ipam_controller_*` | kube-controller-manager metrics, which kind does not scrape. Not node signals |

### Ground truth: never a feature

The snapshots record `kepler_*` and `scaphandre` power, but it is stripped from
the TSDB and only appears in `truth.csv`. power-kind has **synthetic** `kepler_*`
and `scaph_host_power_microwatts` series (`synthetic="true"`), which are linear
in CPU. They are fine for testing wiring, but don't score accuracy against them.

## VM only

Newer cAdvisor and kernel counters, from Kubernetes 1.37 vs. 1.34, not from
virtualization. Not in the training data, so don't use them as features:
`container_cpu_cfs_burst_periods_total`, `container_cpu_cfs_burst_seconds_total`,
`container_creation_time_seconds`, `container_health_state`,
`container_memory_events_high_total`, `container_memory_events_max_total`,
`container_memory_file_dirty_bytes`, `container_memory_file_writeback_bytes`,
`container_memory_pgscan_total`, `container_memory_pgsteal_total`,
`container_memory_workingset_refault_anon_total`,
`container_memory_workingset_refault_file_total`, `machine_cpu_books`,
`machine_cpu_drawers`, `node_memory_CmaFree_bytes`, `node_memory_CmaTotal_bytes`.

## Available in both

### Candidate features

These are time-based counters from `/proc`, `/sys/fs/cgroup` and the scheduler,
the signals a VM still has.

| Family | Metrics | Notes |
|---|---|---|
| CPU time | `node_cpu_seconds_total`, `node_cpu_guest_seconds_total` | per CPU and mode. `mode="steal"` is time the hypervisor gave to other VMs: 0 on bare metal and on this VM so far, non-zero under noisy neighbours |
| Scheduler | `node_schedstat_running_seconds_total`, `node_schedstat_waiting_seconds_total`, `node_schedstat_timeslices_total` | per CPU |
| Kernel activity | `node_context_switches_total`, `node_intr_total`, `node_forks_total`, `node_procs_running`, `node_procs_blocked`, `node_load1`, `node_load5`, `node_load15` | |
| Pressure (PSI) | `node_pressure_cpu_waiting_seconds_total`, `node_pressure_io_waiting_seconds_total`, `node_pressure_io_stalled_seconds_total`, `node_pressure_memory_waiting_seconds_total`, `node_pressure_memory_stalled_seconds_total` | |
| Memory and paging | `node_memory_*` (48), `node_vmstat_pgfault`, `node_vmstat_pgmajfault`, `node_vmstat_pgpgin`, `node_vmstat_pgpgout`, `node_vmstat_pswpin`, `node_vmstat_pswpout`, `node_vmstat_oom_kill` | |
| Disk | `node_disk_read_bytes_total`, `node_disk_written_bytes_total`, `node_disk_reads_completed_total`, `node_disk_writes_completed_total`, `node_disk_io_time_seconds_total`, `node_disk_io_time_weighted_seconds_total`, `node_disk_io_now`, and the other `node_disk_*` time, merge, flush and discard counters (18 in total) | |
| Network | `node_network_receive_bytes_total`, `node_network_transmit_bytes_total`, `node_network_receive_packets_total`, `node_network_transmit_packets_total`, and the other `node_network_*` (36), `node_netstat_*` (42), `node_sockstat_*` (20), `node_softnet_*` (7) | `veth*` interfaces come and go with pods |
| Per container | `container_cpu_usage_seconds_total`, `container_cpu_cfs_periods_total`, `container_cpu_cfs_throttled_periods_total`, `container_memory_*` (10), `container_fs_reads_bytes_total`, `container_fs_writes_bytes_total`, `container_fs_reads_total`, `container_fs_writes_total`, `container_blkio_device_usage_total`, `container_network_*` (8), `container_pressure_*` (6), `container_processes`, `container_threads`, `container_sockets`, `container_oom_events_total` | the basis for splitting power over pods |
| Per process | `namedprocess_namegroup_cpu_seconds_total`, `namedprocess_namegroup_context_switches_total`, `namedprocess_namegroup_major_page_faults_total`, `namedprocess_namegroup_minor_page_faults_total`, `namedprocess_namegroup_read_bytes_total`, `namedprocess_namegroup_write_bytes_total`, `namedprocess_namegroup_memory_bytes`, `namedprocess_namegroup_num_procs`, `namedprocess_namegroup_num_threads`, and the `thread_*` variants | grouped by command name. Covers kubelet, containerd and other work that no pod cgroup accounts for |

### Context only (static or descriptive)

`machine_cpu_cores`, `machine_cpu_physical_cores`, `machine_cpu_sockets`,
`machine_memory_bytes`, `machine_swap_bytes`, `node_dmi_info`,
`node_uname_info`, `node_os_info`, `node_os_version`, `node_boot_time_seconds`,
`node_filesystem_*`, `node_filefd_*`, `node_processes_*`, `node_entropy_*`,
`node_cooling_device_cur_state`, `node_cooling_device_max_state`, `node_arp_entries`,
`node_nf_conntrack_entries`, `node_nf_conntrack_entries_limit`, `node_udp_queues`,
`node_time_*`, `node_timex_*`.

`node_dmi_info` tells the two environments apart: `board_vendor="KVM"` and
`chassis_vendor="QEMU"` in the VM, Supermicro on bare metal.

`machine_nvm_avg_power_budget_watts` exists in both but describes NVDIMM power
budgets, not consumption. Don't use it.

## Things to watch for

- **No `node` label on process metrics.** In both environments,
  `namedprocess_*` series carry only `instance` and `pod`. Map them to a node
  through `kube_pod_info{pod=~"process-exporter-.*"}`.
- **Topology looks different.** In the VM,
  `machine_cpu_cores == machine_cpu_physical_cores` (8 = 8): the guest cannot
  see SMT. The model therefore has no fixed "usage above one half" feature or
  dependence on guest-reported physical cores. CPU fractions are normalized
  by the number of logical CPUs in the execution counters.
- **Node metrics cover the whole VM.** They include everything the guest
  kernel runs, not just pods. In kind, that also means the control-plane
  container and any other Docker containers on the VM.
- **Counters reset.** Pods restart and process groups appear and disappear.
  Handle negative deltas and missing samples, as `CONTRACT.md` asks.
- **Recording rules don't count.** Names with colons (`node_namespace_pod:*`,
  `instance:node_*`) are precomputed aggregations. `CONTRACT.md` says not to
  rely on them.

## Reproduce

```sh
# VM cluster (needs docker, kind, kubectl, helm; binds Prometheus to 127.0.0.1)
git clone https://github.com/Information-System-Engineering/power-kind ../power-kind
# in ../power-kind/kind.yml, add  listenAddress: "127.0.0.1"  under containerPort: 30090
(cd ../power-kind && ./bootstrap.sh)
curl -sG http://127.0.0.1:30090/api/v1/label/__name__/values | jq -r '.data[]' | LC_ALL=C sort -u > vm.txt

# training snapshot
PORT=19095 tools/prom.sh $PWD/snapshots/stress-1008 &
curl -sG http://127.0.0.1:19095/api/v1/label/__name__/values \
  --data-urlencode start=2026-10-08T15:38:59Z --data-urlencode end=2026-10-08T16:23:37Z \
  | jq -r '.data[]' | LC_ALL=C sort -u > bm.txt
kill %1   # then delete the untracked WAL files Prometheus wrote into the snapshot

F='^(node_|container_|machine_|namedprocess_)'
LC_ALL=C comm -12 <(grep -E "$F" bm.txt) <(grep -E "$F" vm.txt)   # both
LC_ALL=C comm -23 <(grep -E "$F" bm.txt) <(grep -E "$F" vm.txt)   # training only
LC_ALL=C comm -13 <(grep -E "$F" bm.txt) <(grep -E "$F" vm.txt)   # VM only
```

Rerun this against the evaluation cluster once we have access to it. Its
configuration may differ from both environments here.
