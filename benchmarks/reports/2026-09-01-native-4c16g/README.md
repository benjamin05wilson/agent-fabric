# Benchmark report: 2026-09-01, native single host

This is the first measured run of Agent Fabric. It found the first scaling
bottleneck, a resource-accounting bug, and the point at which the control plane
collapses, then measured the same tiers again after fixing the first two.
`REPORT.md` in this directory holds every table rendered from the raw JSON
result files that sit alongside it; this page is the narrative.

## Environment

| Item | Value |
|---|---|
| Host | 4 vCPU Intel Xeon @ 2.80 GHz, 16,075 MB RAM, Linux 6.18, no swap |
| Layout | Single host. API, gateway, scheduler, outbox, PostgreSQL 16.13, Redis 7.0.15, MinIO, and the load generator all share the four cores. |
| Baseline revision | `ab0ebb4` (harness only; control-plane behaviour identical to `d2396a6`) |
| Post-fix revision | `ffa65f4` |
| Workload | 10,000 jobs per tier, 100 cpu-millis / 128 MB / 16 PIDs each, seeded 50-500 ms simulated execution, seed 42, no injected failures |
| Fleet | Simulated workers with 8,000 cpu-millis / 16,384 MB / 4,096 PIDs, 5 s heartbeat |
| Admission | `PROJECT_MAX_QUEUED` and `PROJECT_MAX_RUNNING` raised to 1,000,000 so tenant caps do not masquerade as scheduler throughput |
| Budget | 600 s per tier after submission starts; registration budget 120 s (300 s at 10,000) |

The load generator is a single Python process, so at 10,000 workers it
competes for the same cores as the system under test. Numbers at that tier
describe the whole host, not the control plane in isolation.

## Headline results

PLACEHOLDER_HEADLINE

## What the baseline found

### 1. The outbox publisher was starved inside the API process (first bottleneck)

The outbox publisher ran as a background task in the API process. During the
submission burst (10,000 `POST /runs` at 119-149 req/s) the API's event loop was
CPU-bound and the publisher got almost no time. Measured publication lag
during the burst at 100 workers:

| Metric | Value |
|---|---|
| Outbox lag p50 | 20.7 s |
| Outbox lag p99 | 42.7 s |
| Outbox lag max | 43.1 s |
| Lease-offer lag mean | 10.8 s |

Lease offers carry a 10 s acknowledgement deadline. Offers published later
than that were already `LOST` by the time the gateway delivered them, so the
worker's acknowledgement was ignored and the scheduler requeued the run. The
scheduler counter `agent_fabric_lease_expirations_total{acknowledged="false"}`
recorded 951 wasted placements at 100 workers and 1,025 at 1,000 workers,
every one of them inside the first ~90 s.

Fix (`ffa65f4`): the publisher is its own process, `agent-fabric-outbox`,
publishing each batch with one pipelined Redis round trip and one bulk
`UPDATE`, and exporting `agent_fabric_outbox_lag_seconds`. Post-fix lag
stayed between 3 ms and 27 ms through the same burst, and the 100-worker tier
made exactly 10,000 placements for 10,000 runs.

### 2. Scheduler lost updates leaked worker reservations (bug)

`schedule_one` selected candidate workers without locks, then re-selected the
chosen worker `FOR UPDATE`. SQLAlchemy returned the identity-map object it
already had, so the `+=` on `reserved_*` used the stale snapshot and the
`UPDATE` overwrote releases the gateway had committed in between. The audit
sums `reserved_*` across the workers table after every run is terminal; it
should be zero.

| Tier | Runs succeeded | Leaked cpu-millis after drain | Equivalent |
|---|---|---|---|
| 100 workers | 10,000 | 62,200 | 622 phantom jobs, 7.8 workers' full capacity |
| 1,000 workers | 8,570 (not drained) | 68,300 net of 73 in-flight runs | 683 phantom jobs |

At 100 workers five workers read as completely full with nothing running on
them by the end of the run. Fix (`ffa65f4`): the locked re-selects use
`populate_existing=True`. Post-fix leak: 0 at both tiers.

### 3. The gateway saturates on heartbeats at about 4,000 workers (collapse point)

At 10,000 workers the gateway process reached 99.8% of one core before
submission even started. Registration stalled at 3,972 workers; the remaining
6,028 streams failed. Those 3,972 workers heartbeat every 5 s, which is ~800
heartbeats/s, and each heartbeat is a PostgreSQL transaction (`SELECT ... FOR
UPDATE`, `UPDATE workers`) plus a Redis `HSET` on a single asyncio loop.
`pg_stat_statements` recorded 800,341 worker selects and 778,009 worker
updates for that tier. With the loop saturated, lease delivery through the
per-worker `XREAD` tasks fell behind the 10 s deadline: 11,140 of 11,186
placements expired unacknowledged and zero runs reached a terminal state in
988 s. The workers themselves completed 1,021 leases, but every completion
referred to an attempt the scheduler had already marked `LOST`.

This is not addressed by the fixes above and is the next redesign target.
Candidates, in the order they should be measured: batch heartbeat
persistence (keep liveness in Redis, flush `last_seen_at` periodically),
shard the gateway across processes, and replace per-stream blocking `XREAD`
with one consumer fanning out to worker queues.

### 4. The scheduler is serial and plateaus at ~20 placements/s (next bottleneck)

Each placement is one transaction that reloads up to 500 queued runs with
their JSON specs and every healthy worker, then locks two rows. At 100
workers the scheduler used 73% of a core for 20.7 placements/s (48 ms each);
at 1,000 workers 79% for 14.1/s. `pg_stat_statements` attributes 4.1-4.5 ms
of that to the 500-candidate select alone (5.3 million rows returned over the
100-worker tier); the rest is ORM hydration in Python. Fleet size barely
matters, so this is the ceiling for any tier once delivery is fixed, and it
is why end-to-end p50 is dominated by queue wait (202 s post-fix at 100
workers). It is documented here and deliberately not redesigned in this
change.

### 5. Smaller observations

- Registration is fast when the gateway is idle: 100 workers in 32 ms, 1,000
  in 269 ms.
- Healthy-worker count dipped to 96-99 of 100 and 996-999 of 1,000 late in the
  baseline tiers even though every simulated worker heartbeats on time; the
  15 s health window is tight against a gateway that is busy with completions.
- Redis was never the limit at these tiers: ≤ 936 connected clients, 37 MB
  peak, zero rejected connections. The per-stream blocking `XREAD` design
  would need > 10,000 connections at the 10,000-worker tier, which is the
  default `maxclients`; the run never got far enough to hit it.
- Control-plane memory is small: API 120 MB, gateway 107-402 MB (scaling with
  connected streams), scheduler ≤ 100 MB, outbox 86 MB.

## Worker-loss chaos

PLACEHOLDER_CHAOS

## What was not measured

- Hostile sandbox workloads. `tests/security/run_hostile.py` is written but
  requires a Docker host with `runsc`; this environment has neither.
- Scheduler restart, Redis restart, and PostgreSQL interruption. The compose
  smoke script exercises the first two without numbers; they need the same
  audit treatment as worker loss.
- Any tier above 10,000, per the stop conditions in `docs/scaling.md`.

## Reproduce

```bash
python benchmarks/run_native.py --tiers 100,1000 --jobs 10000 --duration 600 --label baseline
python benchmarks/run_native.py --tiers 10000 --jobs 10000 --duration 600 --register-timeout 300 --label baseline
python benchmarks/run_native.py --tiers 1000 --jobs 5000 --kill-fraction 0.1 --kill-after-seconds 10 --label worker-loss
python benchmarks/report.py <results-dir> --output REPORT.md
```
