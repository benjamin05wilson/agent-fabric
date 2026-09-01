# Scheduler

The scheduler filters workers by health, drain state, gVisor capability, and CPU/RAM/PID capacity. Project admission limits prevent a single tenant from filling memory with queued or active work.

Projects accumulate a weighted deficit. Within eligible projects, priority and queue age increase a run's score so low-priority work eventually progresses. Placement chooses the eligible worker with the smallest remaining dominant-resource fraction, producing a best-fit packing policy.

Worker capacity is reserved in the same PostgreSQL transaction that creates an attempt and lease. Completion and lease-expiry paths lock the same rows and clamp counters at zero, making duplicate terminal messages harmless.

This is deliberately a measurable baseline. Sharding, cached indexes, batched heartbeats, and partitioning should follow profiling rather than precede it.

