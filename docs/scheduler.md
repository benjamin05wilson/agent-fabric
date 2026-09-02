# Scheduler

The scheduler filters workers by health, drain state, gVisor capability, and CPU/RAM/PID capacity. Project admission limits prevent a single tenant from filling memory with queued or active work.

Projects accumulate a weighted deficit. Within eligible projects, priority and queue age increase a run's score so low-priority work eventually progresses. Placement chooses the eligible worker with the smallest remaining dominant-resource fraction, producing a best-fit packing policy.

Placement is batched. Each scheduler iteration reads up to `SCHEDULER_CANDIDATE_LIMIT` queued runs (locked with `FOR UPDATE SKIP LOCKED`, so a second scheduler instance would be safe), a projection of every healthy worker's free capacity, the tenant rows, and per-project running counts once; plans up to `SCHEDULER_BATCH_SIZE` placements in memory with the rules above; and writes run transitions, attempts, outbox rows, and worker reservations in bulk inside one transaction. Reservations are relative updates (`reserved = reserved + n`), so the completion and lease-expiry paths, which lock the same rows and clamp counters at zero, compose with them without the scheduler holding worker locks across the batch; the lost-update bug the first benchmark found in the serial scheduler cannot recur because the scheduler no longer writes absolute counters.

Placement is bounded by unacknowledged offers. The scheduler never has more than `SCHEDULER_MAX_OUTSTANDING_OFFERS` leases in the `OFFERED` state, because an offer only becomes work once the gateway has processed the worker's acknowledgement, and an unbounded batch scheduler was measured to out-run the gateway and turn healthy workers' runs into `LOST` ones. The bound is a static setting today; `benchmarks/reports/2026-09-02-batch-scheduler` shows it needs to track the gateway's acknowledgement throughput (500 wasted a third of placements at 1,000 workers, 100 wasted none).

The original serial scheduler placed one run per transaction and reloaded up to 500 candidates plus every healthy worker for each placement, plateauing at 14-21 placements per second regardless of fleet size; both designs and their numbers are in `benchmarks/reports`.

This is deliberately a measurable baseline. Sharding, cached indexes, batched heartbeats, and partitioning should follow profiling rather than precede it.

