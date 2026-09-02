# Benchmark report: 2026-09-02, batch scheduler on the same host

This report measures one architectural change against the tiers in
[`2026-09-01-native-4c16g`](../2026-09-01-native-4c16g/README.md): the serial
scheduler (finding 4 of that report) replaced by batch placement with a bound on
unacknowledged offers. The runner, workload, seed, host, and budget are identical, so
the "before" rows below are that report's `after-fix` rows. `REPORT.md` holds every
table rendered from the raw result files in `results/`; this page is the narrative.
It also carries the first run of the hostile sandbox workloads through a real
`runsc` worker and three fault scenarios beyond worker loss.

## Environment

| Item | Value |
|---|---|
| Host | Same 4 vCPU Intel Xeon, 16,075 MB RAM, Linux 6.18 VM as the previous report, single host, everything sharing the four cores |
| Before revision | `ffa65f4` (that report's `after-fix`: outbox out of the API, reservation leak fixed, serial scheduler) |
| After revision | this branch (batch scheduler, outstanding-offer bound, otherwise identical control plane) |
| Workload | 10,000 jobs per tier, 100 cpu-millis / 128 MB / 16 PIDs, seeded 50-500 ms simulated execution, seed 42 |
| Budget | 600 s per tier after submission starts; registration budget 300 s |
| Scheduler settings | `SCHEDULER_BATCH_SIZE=200`, `SCHEDULER_CANDIDATE_LIMIT=500`, `SCHEDULER_MAX_OUTSTANDING_OFFERS=500` |

## What changed in the scheduler

The serial scheduler placed one run per transaction and, for every placement, re-read
up to 500 queued runs as full ORM rows, every healthy worker, and the per-project
running counts. It plateaued at 14-21 placements per second regardless of fleet size
with the scheduler process at 73-79% of a core, mostly hydrating rows it discarded.

`agent_fabric.scheduler` now reads those inputs once per iteration (candidates are
locked with `FOR UPDATE SKIP LOCKED`, workers are read as a projection of free capacity),
plans up to 200 placements in memory with the same deficit, priority, age, and best-fit
dominant-resource rules, and writes the result in bulk: an `executemany` update of run
states, bulk-inserted attempts and outbox rows, and one relative
`UPDATE workers SET reserved = reserved + n` per worker. Relative reservations mean the
completion and expiry paths, which lock and clamp the same rows, stay correct without
the scheduler holding worker locks across the batch; the lost-update bug fixed in the
previous report cannot recur because the scheduler no longer writes absolute counters.

**Backpressure is the part that mattered.** The first version of the batch scheduler had
no bound on offers in flight. It placed as fast as fleet capacity allowed (each simulated
worker advertises 8,000 cpu-millis against 100-cpu-milli jobs, so 8,000 leases at once)
and immediately overran the gateway, which processes every acknowledgement and heartbeat
as one PostgreSQL transaction on one asyncio loop. Offers expired before their
acknowledgements were processed, heartbeats were processed late so acknowledged attempts
expired too, and retry-safe runs burned their three attempts: in a 100-worker run on an
earlier harness, 24,866 leases expired and **1,127 of 10,000 runs ended `LOST` with every
worker healthy**. A faster scheduler with no admission bound turned a slow gateway into
lost work. The scheduler now counts `OFFERED` attempts before each iteration and never
lets more than `scheduler_max_outstanding_offers` be unacknowledged; the
`agent_fabric_outstanding_offers` gauge exposes the number. An offer only becomes work
once the gateway has processed the acknowledgement, so this pins the scheduler to the
gateway's real throughput instead of the fleet's nominal capacity.

The Go worker was also changed to refuse a lease that arrives after its own deadline
instead of running it, which closes a duplicate-execution window (the previous report's
"wasted lease offers" were executed by the simulated workers and then ignored by the
gateway).

## Headline results

| Workers | Scheduler | Drained | Drain (s) | Completions/s | Placements for 10,000 runs | Wasted offers | Runs lost | Time-to-start p50 / p99 | Gateway / scheduler CPU (% of a core) |
|---|---|---|---|---|---|---|---|---|---|
| 100 | serial (before) | yes | 478 | 20.8 | 10,000 | 0 | 0 | 202 s / 403 s | 35 / 74 |
| 100 | batch, bound 500 | **yes** | **110** | **90.8** | 10,000 | 0 | 0 | **23.5 s / 40.1 s** | 93 / 41 |
| 1,000 | serial (before) | no (557 open at 683 s) | n/a | 14.0 | 9,764 placed | 185 | 0 | 306 s / 593 s | 92 / 79 |
| 1,000 | batch, bound 500 | **yes** | **284** | 50.4 | 14,666 | 4,666 (32%) | 0 | 114 s / 217 s | 99 / 32 |
| 1,000 | batch, bound 100 | **yes** | **274** | 36.5 | 10,000 | **0** | 0 | 118 s / 207 s | 98 / 16 |
| 10,000 | serial (before) | collapse (4,233 registered, 0 finished) | n/a | 0 | 4,183 | 4,143 | 0 | never | 100 / 62 |
| 10,000 | batch | collapse (3,712 registered, 0 finished) | n/a | 0 | 30,885 | 30,385 | 0 | never | 101 / 23 |

"Completions/s" is completed runs over the drain window; "wasted offers" are placements
whose offer expired unacknowledged after 10 s and were requeued. Before rows are from
`../2026-09-01-native-4c16g/results/after-fix-*.json`; every other row is in `results/`.
The batch rows were recorded from this branch's working tree before it was committed, so
their result files carry the previous report's revision with `git_dirty: true`; the
scheduler settings are listed in each file's `runner_configuration`.

### 100 workers

- Drained in **110 s instead of 478 s** (4.3x). Placement rose from 20.9 to 90.8 per
  second; the scheduler made exactly 10,000 placements for 10,000 runs and 1,638
  batch iterations averaging 46 ms each.
- Time-to-start p50 fell from 202 s to 23.5 s and p99 from 403 s to 40.1 s; end-to-end
  p50 24.0 s, p99 40.4 s. No run was lost, none needed a retry, and the reservation
  audit was zero (no leak, nothing in flight) after the run.
- The gateway is now the limit: 93% of a core acknowledging and completing roughly 100
  leases per second through one transaction each, while the scheduler dropped to 41%
  and spent most of its iterations waiting on the offer bound (the gauge sat at 496-500).
  PostgreSQL's top statements are now the gateway's worker lookups and the API's
  admission count, not the scheduler's candidate scan (1,638 calls instead of 10,000).

### 1,000 workers

- **Drained in 284 s**; the serial scheduler never drained this tier (557 runs still
  open when the 600 s budget ended). Placement rose from 14.3 to 50.4 completions per
  second. Time-to-start p50 fell from 306 s to 114 s and p99 from 593 s to 217 s. No run
  was lost and the reservation audit was zero.
- **The offer bound is too loose for this gateway.** With 1,000 streams heart-beating
  (200 transactions per second before any lease traffic) the gateway ran at 99% of a core
  and could not acknowledge 500 outstanding offers inside the 10 s deadline: 4,666 of
  14,666 offers (32%) expired unacknowledged and were requeued, and 2,557 runs needed a
  second attempt. Nothing was lost, because an unacknowledged offer is always safe to
  requeue, but a third of the scheduler's work at this tier was wasted. The bound is a
  static knob today; it should be driven by measured acknowledgement latency, which is
  the obvious next change and is left unimplemented here so the number is on record.

### 1,000 workers with the bound at 100 (`batch-bound100`)

Re-running the same tier with `SCHEDULER_MAX_OUTSTANDING_OFFERS=100` confirms the
diagnosis: **10,000 placements for 10,000 runs, zero expired offers, zero retries**, and
the tier still drained in 274 s (p50 time-to-start 118 s, p99 207 s). Drain time is the
same as with the bound at 500 because the gateway, at 97% of a core, is the throughput
limit either way; the lower bound simply stops the scheduler offering work the gateway
cannot acknowledge in time. A bound derived from measured acknowledgement latency would
make this automatic across fleet sizes.

### 10,000 workers

Unchanged: the tier still collapses at the gateway, exactly as finding 3 of the previous
report describes, and the scheduler change neither helps nor hurts it. 3,712 workers
registered (6,288 streams failed while the gateway was saturated), the gateway pinned one
core for the entire 970 s, and no run finished. The batch scheduler kept up to 400-500
offers out to the workers that had registered, but their acknowledgements were never
processed in time, so every offer expired (30,385 expirations, all requeued, nothing
lost). This tier is a gateway measurement, not a scheduler one: per-heartbeat
persistence in a single process is the next redesign target.

## Fault scenarios beyond worker loss

`tests/chaos/run_scenarios.py` runs a 200-worker simulated fleet, submits 2,000 retry-safe
0.5 to 2 s jobs, waits until at least 20 leases are in flight, injects one fault, and then
computes every figure from PostgreSQL timestamps: detection is fault time to the first
affected attempt being marked `LOST`, recovery is fault time to the last affected run
reaching a terminal state, lost runs are runs that ended `LOST`, unfinished runs are what
remained after a 300 s deadline. The control plane is restarted fresh before every scenario.
Raw files: `results/chaos/`. The same five scenarios were also run against `main`
(the serial scheduler with the outbox still inside the API) on an earlier harness; those
numbers are quoted where they differ.

| Scenario | Fault | Affected runs | Detection | Requeued | Recovery | Runs lost | Unfinished at 300 s |
|---|---|---|---|---|---|---|---|
| kill-worker | one worker holding 70 running attempts dropped | 82 | 9.9 s | 82 | 22.3 s | 0 | 0 |
| kill-fleet-10pct | 20 workers holding 249 running attempts dropped | 608 | 9.6 s | 608 | 23.6 s | 0 | 0 |
| scheduler-restart | scheduler process killed and restarted | 0 | n/a | 0 | placement gap 1.4 s | 0 | 0 |
| postgres-restart | `pg_ctl restart -m fast` mid-run | 468 | 9.2 s | 468 | 24.1 s | 0 | 0 |
| redis-restart | Redis stopped and restarted mid-run | 500 | 9.4 s | **never** | never | 0 | **1,583 of 2,000** |

- **Worker loss costs nothing but time.** One worker or 10% of the fleet dropped mid-run:
  every affected run was requeued and finished on a surviving worker, detection sits at the
  10 s offer deadline, recovery at 22-24 s, and no run needed a third attempt. Recovery is
  unchanged from the serial scheduler (22.0 s and 22.6 s on `main`).
- **The scheduler still leases to a dead worker for up to 15 s.** Of the 671 leases the 20
  killed workers lost, 250 were work they were running; the other 421 were offers placed on
  them after they died, because the gateway does nothing when a stream closes and the
  worker stays "healthy" until its last heartbeat is 15 s old. The batch scheduler makes
  this more visible than the serial one did (161 such offers on `main`) because it fills a
  dead worker's capacity within one iteration. Acting on stream closure is the fix.
- **Scheduler and PostgreSQL restarts are routine.** A scheduler restart is a 1.4 s pause in
  placements. A fast-mode PostgreSQL restart made the API answer 500 for 44 submissions
  (the earlier harness retried under the same idempotency keys; this one records them as
  submission errors), expired 535 leases whose acknowledgement or heartbeat hit the outage,
  and everything finished 24 s after the fault with nothing lost.
- **Redis restart still livelocks the control plane, for a second reason.** On `main` the
  outbox publisher inside the API died on its first failed `XADD`; the previous report's
  standalone publisher survives (it backed off and resumed, 0 unpublished rows). But every
  gateway stream's dispatcher task, blocked in `XREAD`, dies with the connection and is
  never recreated, so no connected worker receives another lease offer. The scheduler keeps
  offering to workers it believes healthy (they still heartbeat), every offer expires
  (14,040 expirations in 300 s), and 1,583 of 2,000 runs were still queued at the deadline.
  Nothing was lost or corrupted; the system needs a gateway restart or the workers to
  reconnect. The fix is a reconnecting dispatcher (or a supervised one per stream); it is
  left for the next change so the number is on record.

## Hostile workloads through the real gVisor worker

First execution of `tests/security/run_hostile.py`: the Go worker (Docker 29.3 with
`runsc` from the gVisor release channel, systrap platform, no KVM) registered with the
control plane on the benchmark host and each workload was submitted through the public API
against the public `octocat/Hello-World` repository. Raw files: `results/sandbox/`.

| Workload | Limits | Outcome | Contained | Evidence |
|---|---|---|---|---|
| escape_probe | 128 MB, 16 PIDs | `SUCCEEDED` in 2 s | yes | uid/gid 65532, `CapEff`/`CapBnd` all zero, kernel `4.19.0-gvisor`, `/etc`, `/usr`, `/` denied (EACCES), `/workspace` writable, no Docker socket, 16 device nodes |
| memory_bomb | 128 MB | `FAILED`, exit 137 after 64 MiB allocated | yes | OOM-killed by the memory limit |
| fork_bomb | 256 MB, 32 PIDs | `FAILED`, exit 2, no output | yes | gVisor terminates the sandbox at the PID limit (see below) |
| infinite_loop | 10 s timeout | `TIMED_OUT` at 12.1 s | yes | worker's wall-clock deadline fired, container removed |
| tmp_exhaustion | 64 MiB tmpfs | `FAILED`, exit 1 after 64 MiB | yes | `OSError: [Errno 28] No space left on device` at exactly the tmpfs size (rerun after the worker restart) |
| forbidden_network | `--network=none` | `FAILED`, exit 4 in 4 s | yes | HTTPS: name resolution failed; raw TCP to 1.1.1.1:53: `Network is unreachable` (rerun after the worker restart) |
| disk_exhaustion | 128 MB "disk" | **`LOST` after 56 s and 12.5 GB written** | **no** | see below |

Two findings came out of running this for real:

- **gVisor needs PID headroom, and enforces the limit by killing the sandbox.** The first
  attempt failed every workload except the memory bomb at container creation
  (`waiting for sandbox to start: EOF`, Docker exit 125): with `--pids-limit=16` the
  Sentry and gofer threads, which share the container's pids cgroup, cannot start; 32 and
  64 work. The worker now adds a measured overhead of 48 to the lease's PID budget and
  reports Docker's exit status 125 as `SANDBOX_START` instead of as the workload's exit
  code. When a workload then exhausts the limit, gVisor terminates the whole sandbox with
  exit 2 and no output rather than failing `fork()` with `EAGAIN` as runc does (measured
  side by side); the run is contained but gets no chance to report.
- **Disk exhaustion is not contained, and the failure cascades.** The workspace bind mount
  has no quota (a documented limitation), so the workload wrote 12.5 GB in 56 s and filled
  the host. MinIO then refused the run's log objects (`XMinioStorageFull`), the gateway's
  event handler raised on that write and closed the worker's stream, the worker exited
  with `EOF` **without running its deferred workspace cleanup**, its running lease expired
  and the run was marked `LOST`, and 28 GB of workspace survived on disk until it was
  removed by hand. The two workloads queued behind it never ran until the worker was
  restarted. Two things changed as a result: the worker now purges stale per-attempt
  workspaces when it starts, and the gateway's behaviour of dropping a worker stream
  because the log store is full is recorded as the next fix. The quota itself still needs
  a quota-enabled filesystem or per-run loop device on the worker host.

## Next steps, in order

1. **Batch heartbeat persistence in the gateway.** Coalesce heartbeats into one
   `UPDATE ... WHERE id = ANY(...)` per second and refresh attempt expiries in one
   statement. The 10,000-worker tier is a gateway measurement at every revision so far.
2. **Drive the offer bound from acknowledgement latency** instead of a static setting,
   so 1,000 workers do not need hand-tuning from 500 to 100.
3. **Act on stream closure and reconnect dispatchers.** Mark a worker unhealthy when its
   stream ends (the 10% fleet-loss run wasted 421 offers on dead workers) and recreate the
   per-stream Redis dispatcher after a Redis restart (the livelock above).
4. **Never drop a worker stream because the log store is full**; degrade log persistence
   instead. That single behaviour turned a disk-exhaustion workload into a lost run and a
   dead worker.
5. **Workspace quota** on the worker host (project quotas or a per-run loop device).
6. Apply `infra/terraform` to a real account and repeat the 100-worker tier there with real
   gVisor workers.
7. Firecracker only after all of the above.
