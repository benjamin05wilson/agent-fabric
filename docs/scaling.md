# Scaling methodology

## Frozen portfolio baseline

The `v0.1.0-portfolio` performance baseline is frozen at **50,000 simulated
workers represented by durable PostgreSQL rows and simultaneous real gRPC
streams**. The 100,000-worker Docker Desktop attempt failed around 43.8k active
streams and is retained as boundary evidence, not a capability claim or a
benchmark to keep repeating on the same host.

Do not extend the public scale claim beyond 50,000 without moving to a properly
tuned Linux host, recording its kernel/network/file-descriptor limits, and
publishing a fresh reproducible evidence set. New changes should first reproduce
the existing 50k acceptance workload.

Run identical deterministic workloads at 10,000, 25,000, and 50,000 simulated
workers. Above 10,000, shard the load generator so the client process does not
become the measured ceiling. Record the Git revision, seed, host CPU/RAM,
container limits, service versions, job mix, simulated durations, and injected
failure rates.

Report scheduling throughput, completion throughput, queue depth, p50/p95/p99 scheduling and end-to-end latency, control-plane RSS, PostgreSQL and Redis latency, heartbeat delay, lease expirations, detection/recovery time, and job loss. Averages alone are insufficient.

Stop escalation if memory exceeds 80% of the assigned environment, swap activity materially affects results, error rate exceeds 1%, or the control plane cannot recover. A tier only counts when its workers are durable PostgreSQL rows and simultaneously active gRPC streams. Do not extrapolate beyond a measured acceptance tier.

The first bottleneck is the first repeatable throughput plateau or disproportionate tail-latency/resource increase. Preserve the baseline report and profile before proposing a redesign.

For scheduler scaling, prefill the queue while all schedulers are stopped, then
start 1, 2, 4, or 8 processes so API submission is outside the timed drain. The
runner preserves one worker fleet across phases and records retry offers and
duplicate acknowledged executions separately:

```powershell
python benchmarks/run_scheduler_scale.py --workers 50000 --fleet-shards 8 `
  --jobs 10000 --replicas 1,2,4,8 --duration 1200
```

Do not describe an unacknowledged expired offer as duplicate execution. Both are
important, but the former tests outbound delivery/lease recovery while the latter
tests scheduler exclusion correctness.
