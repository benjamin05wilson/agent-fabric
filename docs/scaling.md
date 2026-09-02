# Scaling methodology

Run identical deterministic workloads at 10,000, 25,000, 50,000, and 100,000
simulated workers. Above 10,000, shard the load generator so the client process
does not become the measured ceiling. Record the Git revision, seed, host
CPU/RAM, container limits, service versions, job mix, simulated durations, and
injected failure rates.

Report scheduling throughput, completion throughput, queue depth, p50/p95/p99 scheduling and end-to-end latency, control-plane RSS, PostgreSQL and Redis latency, heartbeat delay, lease expirations, detection/recovery time, and job loss. Averages alone are insufficient.

Stop escalation if memory exceeds 80% of the assigned environment, swap activity materially affects results, error rate exceeds 1%, or the control plane cannot recover. A tier only counts when its workers are durable PostgreSQL rows and simultaneously active gRPC streams. The 100,000 tier is an experiment, not an acceptance claim; do not extrapolate to one million.

The first bottleneck is the first repeatable throughput plateau or disproportionate tail-latency/resource increase. Preserve the baseline report and profile before proposing a redesign.
