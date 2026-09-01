# Scaling methodology

Run identical deterministic workloads at 100, 1,000, and 10,000 simulated workers. Record the Git revision, seed, host CPU/RAM, container limits, service versions, job mix, simulated durations, and injected failure rates.

Report scheduling throughput, completion throughput, queue depth, p50/p95/p99 scheduling and end-to-end latency, control-plane RSS, PostgreSQL and Redis latency, heartbeat delay, lease expirations, detection/recovery time, and job loss. Averages alone are insufficient.

Stop escalation if memory exceeds 80% of the assigned environment, swap activity materially affects results, error rate exceeds 1%, or the control plane cannot recover. The 100,000 and 1,000,000 tiers are opt-in experiments, not acceptance claims.

The first bottleneck is the first repeatable throughput plateau or disproportionate tail-latency/resource increase. Preserve the baseline report and profile before proposing a redesign.

