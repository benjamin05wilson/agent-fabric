# Sharded gateway scale experiment

Run on 2 September 2026 on a 24-vCPU, 62.5-GiB Docker Desktop host. This is
the V2 rerun of the unchanged V1 workload that previously plateaued at 3,898
workers on one Python gateway with one Redis reader per worker.

## Result

Eight Python gateway processes behind HAProxy, each with one supervised Redis
consumer and a local worker connection registry, passed the original 10,000
worker acceptance criterion. All 10,000 real gRPC streams registered durably in
11.231 seconds. The 10,000 submitted jobs all succeeded with no retry, lost
attempt, stream error, unpublished outbox event, or resource leak.

At 10,000 workers Redis had 56 clients rather than approximately one client per
worker. Measured heartbeat throughput was 1,835.748/s over the full run (the
steady cadence is approximately 2,000/s), placement throughput was 43.836/s,
and steady-state gateway message p95/p99 was 0.804/22.536 ms. The eight shards
held exactly 1,250 connections each. Their combined peak was 954.52% CPU and
1,298.4 MiB RSS; PostgreSQL peaked at 161.78%, Redis at 16.56%, and HAProxy at
89.12% during the registration burst.

## Higher tiers

The single Python load generator became the next artificial limit at 23,682
connections. Splitting the same simulated fleet across independent generators
removed that client-side bottleneck.

| Tier | Durable active streams | Registration | Workload result |
|---:|---:|---:|---|
| 10,000 | 10,000 | 11.231 s | 10,000/10,000 succeeded; zero loss/retry/error |
| 25,000 | 25,000 | 11.972 s | 10,000/10,000 succeeded; zero loss/retry |
| 50,000 | 50,000 | 31.125 s | 10,000/10,000 succeeded; zero loss/retry |
| 100,000 | ~43,800 peak | unstable | failed; Docker API degraded and streams fell back toward 41,000 |

At 25,000 and 50,000 workers the single scheduler process saturated one CPU
core and controlled job drain time. At 50,000 workers the eight gateways used
about 4.0 GiB RSS and 4.4-8.6 CPU cores depending on phase, while the 5-second
heartbeat cadence generated approximately 10,000 heartbeats/s. HAProxy also
approached one core. The 50,000 connection storm caused transient reconnects,
but every worker became durably active and the complete workload passed.

The 100,000 tier is deliberately not claimed. During the sixteen-generator
connection storm Docker Desktop's management API returned HTTP 500 for exec and
stats operations. Active streams peaked around 43,800 and then declined despite
worker reconnects. A Linux host with kernel/conntrack telemetry and ramped
registration is required to separate Docker Desktop networking limits from the
next service limit.

## Redis restart recovery

Redis was restarted during a 100-worker, 1,000-job workload. The shard reader
supervisors reconnected and recreated consumer groups; all 1,000 jobs succeeded
with zero retry, loss, stream error, unpublished event, or leaked reservation.

## Next bottleneck

The next server-side performance work is scheduler parallelism and placement
throughput, followed by load-balancer scaling or direct shard routing. The
per-worker Redis reader architecture is no longer the limiting factor.

