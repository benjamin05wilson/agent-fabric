# Scheduler

The current scheduling plane supports multiple cooperating processes. It keeps
candidate ownership and expensive placement planning parallel while
serializing only the short commit phase that protects global invariants.

## Eligibility and placement

A worker is eligible when it is healthy, not draining, advertises the gVisor
backend, satisfies required capabilities, and has enough unreserved CPU,
memory, PIDs, GPU devices, and VRAM. CPU-only work prefers non-GPU workers so
ordinary jobs do not consume scarce accelerator capacity.

Each scheduler maintains a process-local weighted deficit for projects.
Project weight, run priority, and queue age determine candidate order, which
prevents a continuously busy high-priority tenant from permanently starving
older work. Eligible workers are scored by the dominant resource fraction that
would remain after placement. Equal-capacity workers are shuffled, and a batch
spreads its first offers across streams before assigning a second offer to one
worker.

## Parallel planning

An iteration claims up to `SCHEDULER_CANDIDATE_LIMIT` queued runs using
`FOR UPDATE SKIP LOCKED`; concurrent replicas therefore receive disjoint
candidate batches. Each replica reads at most `SCHEDULER_WORKER_LIMIT` healthy
workers through an independently seeded, rotating keyset window instead of
loading the full fleet. It plans up to `SCHEDULER_BATCH_SIZE` placements against
an in-memory capacity view.

Planning deliberately uses an unlocked snapshot and performs no writes. This
keeps its database scans and CPU work outside the serialized section. Snapshot
decisions are provisional until the commit phase revalidates them.

## Correct commit protocol

`FOR UPDATE SKIP LOCKED` protects run ownership, but it cannot by itself protect
the global outstanding-offer ceiling or exact per-project running limits.
Replicas therefore call `pg_try_advisory_xact_lock` with a shared transaction
lock key. A replica that does not acquire it exits immediately, releases its
candidate rows, and retries on its next poll.

The winning replica:

1. recounts `OFFERED` attempts and per-project running runs;
2. trims the provisional batch to the exact global and tenant limits;
3. locks selected worker rows in stable ID order;
4. rechecks health, drain state, gVisor, capabilities, and every reservation
   dimension against current values;
5. stamps the acknowledgement deadline; and
6. atomically persists attempts, run transitions, lease-offer outbox rows, and
   worker reservations.

This lock ordering avoids the measured Run → advisory lock → Worker → Run cycle
with concurrent acknowledgements. Exact revalidation prevents capacity
overcommit when independently planned batches select the same worker.

## Backpressure and recovery

The scheduling plane never intentionally exceeds
`SCHEDULER_MAX_OUTSTANDING_OFFERS` attempts in `OFFERED`. A lease consumes work
only after its gateway delivers it and the worker acknowledges it. Without the
bound, placement can outrun gateway acknowledgement throughput and expire
offers before healthy workers see them.

The default ceiling is 100, based on the batch-scheduler measurements: 500
wasted roughly a third of placements at 1,000 workers, while 100 completed
without waste. The commit-time recount makes that ceiling global across
replicas. Lease expiry locks attempts with `SKIP LOCKED`, releases resource
reservations, and either requeues or marks the run lost according to
acknowledgement and retry-safety state.

## Measured behavior

The original one-run-per-transaction scheduler plateaued at roughly 14–21
placements/s. Batch planning removed that ceiling. On the frozen 50,000-worker,
10,000-job workload, one, two, and four replicas completed correctness-clean at
67.18, 91.97, and 82.78 jobs/s. Eight replicas fell to 63.62 jobs/s and created
62 expired unacknowledged offers.

Two replicas are therefore the measured throughput peak on this host. Four
replicas demonstrate coordination correctness, not a linear speedup claim, and
eight fail the zero-retry acceptance gate. The exact protocol and evidence are
in [`benchmarks/reports/2026-09-02-parallel-scheduler`](../benchmarks/reports/2026-09-02-parallel-scheduler/README.md).

The default Compose profile starts one scheduler. The `scheduler-scale` profile
adds seven optional replicas for controlled experiments; it is not a production
replica recommendation.
