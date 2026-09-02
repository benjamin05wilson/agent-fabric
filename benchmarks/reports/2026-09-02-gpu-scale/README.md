# GPU scheduling and gateway scale experiment

Run on 2 September 2026 on a 24-vCPU, 62.5-GiB Docker Desktop host. The raw
runner output remains in `benchmarks/results/`; the compact evidence used for
this report is committed as `evidence.json`.

## Result

The 10,000-worker tier did not register successfully. Registration stopped
making progress at 3,898 durable, active streams for more than two minutes, so
the 25,000, 50,000, and 100,000 tiers were deliberately not attempted.

At the plateau the single Python gRPC gateway consumed roughly one full CPU
core (peak 112.86%) and 321.3 MiB RSS. PostgreSQL peaked at 33.92% CPU during
the initial registration burst, then was normally in the 0.5-3.3% range.
Redis peaked at 10.81% CPU and held about 3,901 clients. The measured heartbeat
rate was 725.735/s overall and approximately 780/s at the stable plateau. The
gateway p99 histogram reached its 1-second upper bucket during registration.

This makes the next bottleneck the gateway's per-stream/per-message Python
execution and connection architecture, not host memory or PostgreSQL query
capacity. The next scale change should shard the gateway and its Redis stream
readers before claiming a 10,000-worker tier.

## Mixed CPU/GPU fleet

The corrected mixed run used 90 CPU-only workers, 10 simulated CUDA workers
with one 8-GiB GPU each, 400 ordinary jobs, and 100 jobs requesting
`gpu: 1`, `vram_mb: 8192`, and the `cuda` capability. Jobs ran for a seeded
2-4 seconds.

| Measurement | Result |
|---|---:|
| Terminal runs | 500/500 succeeded |
| Lost attempts / retries | 0 / 0 |
| GPU leases | 100 |
| GPU jobs placed on CPU workers | 0 |
| CPU jobs placed on GPU workers | 0 |
| Peak GPU reservation | 10/10 |
| Mean sampled GPU reservation | 7.0/10 |
| Peak CPU / GPU queue | 54 / 80 |
| CPU time-to-start p50 / p99 | 650 ms / 1.22 s |
| GPU time-to-start p50 / p99 | 14.86 s / 29.23 s |
| Gateway p95 / p99 at completion | 86 ms / 228 ms |
| Gateway / PostgreSQL / Redis peak CPU | 80.88% / 22.99% / 2.25% |
| Gateway RSS peak | 80.25 MiB |

No class starved and scarce GPU capacity was preserved for GPU jobs. The GPU
queue waited longer because 100 GPU jobs shared ten single-GPU workers; those
workers remained saturated for most of the GPU phase. The first long mixed run
exposed a lock-order deadlock between batched heartbeat renewal and completion.
Separating worker liveness from ordered, skip-locked attempt renewal eliminated
the deadlock and all resulting lease loss in this rerun.

## Worker-loss recovery

The recovery run submitted 200 jobs lasting 10-15 seconds to 50 workers and
killed the five busiest workers. The scheduler's packing policy had placed all
200 in-flight attempts on those five workers (40 per worker). All were marked
`LOST`, retried, and eventually succeeded elsewhere.

| Measurement | Result |
|---|---:|
| Interrupted attempts | 200 |
| Requeued and succeeded | 200 |
| Permanently lost runs | 0 |
| Detection p50 / p99 | 12.14 s / 12.65 s |
| Recovery p50 / p99 | 24.63 s / 27.58 s |
| Gateway / PostgreSQL / Redis peak CPU | 40.36% / 12.44% / 1.77% |
| Gateway p99 at completion | 195 ms |

Recovery is correct, but concentrating the entire workload on 10% of the fleet
increases the blast radius. A configurable spread or anti-concentration policy
is the next scheduler resilience improvement.

