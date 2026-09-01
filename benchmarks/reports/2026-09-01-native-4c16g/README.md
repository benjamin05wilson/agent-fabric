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
| Post-fix revision | `ffa65f4` (tiers), `914f675` (chaos: same control plane, simulator heartbeat fix) |
| Workload | 10,000 jobs per tier, 100 cpu-millis / 128 MB / 16 PIDs each, seeded 50-500 ms simulated execution, seed 42, no injected failures |
| Fleet | Simulated workers with 8,000 cpu-millis / 16,384 MB / 4,096 PIDs, 5 s heartbeat |
| Admission | `PROJECT_MAX_QUEUED` and `PROJECT_MAX_RUNNING` raised to 1,000,000 so tenant caps do not masquerade as scheduler throughput |
| Budget | 600 s per tier after submission starts; registration budget 120 s (300 s at 10,000) |

The load generator is a single Python process, so at 10,000 workers it
competes for the same cores as the system under test. Numbers at that tier
describe the whole host, not the control plane in isolation.

## Headline results

| Workers | Revision | Drained in budget | Placements/s | Wasted lease offers | Reservation leak after run | Time-to-start p50 / p99 | End-to-end p50 / p99 | Gateway / scheduler CPU |
|---|---|---|---|---|---|---|---|---|
| 100 | baseline | yes, 529 s | 20.7 | 951 of 10,951 (8.7%) | 62,200 cpu-millis = 622 phantom jobs | 257 s / 457 s | 257 s / 457 s | 32% / 73% |
| 100 | after fix | yes, 478 s | 20.9 | 0 of 10,000 | 0 | 202 s / 403 s | 202 s / 404 s | 35% / 74% |
| 1,000 | baseline | no, 1,430 open at 684 s | 14.1 | 1,025 of 9,673 (10.6%) | 68,300 cpu-millis net of in-flight | 345 s / 605 s | 345 s / 605 s | 92% / 79% |
| 1,000 | after fix | no, 557 open at 683 s | 14.3 | 185 of 9,764 (1.9%) | 0 | 306 s / 593 s | 307 s / 593 s | 92% / 79% |
| 10,000 | baseline | collapse: 3,972 of 10,000 registered, 0 runs finished in 988 s | 11.3 (all wasted) | 11,140 of 11,186 | 0 | never | never | 100% / 82% |
| 10,000 | after fix | collapse: 4,233 of 10,000 registered, 0 runs finished in 993 s | 4.2 (all wasted) | 4,143 of 4,183 | 0 | never | never | 100% / 62% |

"Wasted lease offers" are placements whose offer expired unacknowledged after
10 s and were requeued; every one costs a scheduler transaction and 10 s of
queue time for that run. CPU is the process's share of one core over the
tier. Throughput is unchanged by the fix because the serial scheduler (finding
4) and, at 1,000 workers, the gateway (finding 3) are the limits; what
changed is that placements stopped being wasted, reservations stopped
leaking, and queue wait fell accordingly (p50 end-to-end -55 s at 100
workers, -39 s at 1,000).

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

- 185 offers (1.9%) still expired unacknowledged at 1,000 workers after the
  fix, all within the first two minutes, while outbox lag never exceeded
  0.24 s. During the submission burst the API, gateway (92% of a core),
  scheduler, and load generator saturate the host's four cores together, so
  delivery through the gateway's per-worker `XREAD` tasks can miss the 10 s
  deadline. This is host contention plus finding 3, not the outbox.
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

Both scenarios: 1,000 simulated workers, 3,000 jobs of 20-40 s, submission
finished in ~22 s, 10% of the fleet (100 workers) killed 60 s after
submission by cancelling their streams with no completion, cleanup, or
further heartbeat. Post-fix revision. Detection is the kill-to-`LOST` delay
recorded by the scheduler's reconcile loop; recovery is kill-to-terminal-state
for the affected run, which includes waiting in the queue behind other work
and re-executing a 20-40 s job from scratch.

| Scenario | In-flight on killed workers at kill | Attempts marked LOST | Runs affected | Requeued and finished elsewhere | Runs LOST | Detection p50 / p95 / max | Recovery p50 / p95 / p99 / max | Drained | Leak |
|---|---|---|---|---|---|---|---|---|---|
| Random 10% | 136 | 174 (146 running, 28 offered after the kill) | 160 | 174 | 0 | 13.2 s / 18.5 s / 19.2 s | 50.8 s / 61.0 s / 61.3 s / 61.6 s | yes, 235 s | 0 |
| Busiest 10% first | 473 | 629 (468 running, 161 offered after the kill) | 597 | 629 | 0 | 12.0 s / 17.0 s / 22.1 s | 61.2 s / 81.0 s / 85.6 s / 90.3 s | yes, 270 s | 0 |

Reading the numbers:

- Detection lands where the configuration says it should. A running lease is
  extended to `now + 15 s` on every heartbeat, so a worker that dies just after
  heartbeating is detected ~15 s later plus reconcile-loop latency; offers
  sent to a dead worker before the scheduler notices expire after the 10 s
  acknowledgement deadline. Because the scheduler keeps treating a dead
  worker as healthy for up to 15 s, it kept placing on the victims after the
  kill: 28 such offers in the random case and 161 in the busiest case, all
  recovered.
- Every affected run finished. `retry.safe_on_worker_loss` was set with
  `max_attempts: 3`; no run needed more than two attempts.
- The reservation audit is zero after both runs: the resources held by the
  dead workers were released by lease expiry and nothing was double-counted.
- Recovery time is dominated by re-execution and queue wait, not by
  detection: a 20-40 s job restarted from scratch behind a queue that the
  scheduler drains at ~13/s. Recovering 629 attempts (busiest case) added
  about 35 s to the drain compared with the random case.
- The first attempt at this scenario, with the default 50-500 ms jobs,
  measured nothing: only a handful of attempts are ever in flight when the
  scheduler is the bottleneck, and best-fit packing puts them on one or two
  workers, so a random 10% kill hit none. The `busiest` selection and the
  long-job workload exist because of that. A second attempt exposed a
  simulator defect: the simulated worker only heartbeated when its stream was
  idle for 5 s, unlike the Go worker's fixed ticker, so a worker fed a steady
  stream of leases went silent and its running leases expired mid-job. Both
  are harness fixes; neither changed the control plane.

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
python benchmarks/run_native.py --tiers 100,1000 --jobs 10000 --duration 600 --label after-fix
python benchmarks/run_native.py --tiers 1000 --jobs 3000 --duration 900 --min-duration-ms 20000 --max-duration-ms 40000 \
  --kill-fraction 0.1 --kill-after-seconds 60 --kill-selection random --label worker-loss-random
python benchmarks/run_native.py --tiers 1000 --jobs 3000 --duration 900 --min-duration-ms 20000 --max-duration-ms 40000 \
  --kill-fraction 0.1 --kill-after-seconds 60 --kill-selection busiest --label worker-loss-busiest
python benchmarks/report.py <results-dir> --output REPORT.md
```
