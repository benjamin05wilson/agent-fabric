# Scheduler

The scheduler filters workers by health, drain state, gVisor capability, and CPU/RAM/PID capacity. Project admission limits prevent a single tenant from filling memory with queued or active work.

Projects accumulate a weighted deficit. Within eligible projects, priority and queue age increase a run's score so low-priority work eventually progresses. Placement chooses the eligible worker with the smallest remaining dominant-resource fraction, producing a best-fit packing policy.

Worker capacity is reserved in the same PostgreSQL transaction that creates an attempt and lease. Candidate workers are read without locks; the chosen worker and run are then re-selected `FOR UPDATE` with `populate_existing` so the locked values, not the earlier snapshot, are incremented. Without that refresh the scheduler overwrote releases committed by the gateway between the two reads, and the first benchmark measured 62,200 cpu-millis of phantom reservations after 10,000 successful runs on 100 workers. Completion and lease-expiry paths lock the same rows and clamp counters at zero, making duplicate terminal messages harmless.

The scheduler places one run per transaction and reloads up to 500 queued candidates plus every healthy worker for each placement. Measured on a 4-core host this plateaus at roughly 14-21 placements per second regardless of fleet size; it is the next bottleneck after the outbox publisher and is documented, not yet redesigned, in `benchmarks/reports`.

This is deliberately a measurable baseline. Sharding, cached indexes, batched heartbeats, and partitioning should follow profiling rather than precede it.

