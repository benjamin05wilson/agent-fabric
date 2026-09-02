# Failure semantics

Delivery is at least once. Effects are not claimed to be exactly once.

- An unacknowledged expired offer is safely requeued.
- An acknowledged attempt is considered uncertain when heartbeats stop.
- Uncertain execution is retried only when the caller explicitly declares it safe and has remaining attempts; otherwise the run becomes `LOST`.
- Repeated acknowledgements, events, completions, cleanup confirmations, API submissions, and cancellations are idempotent.
- PostgreSQL is authoritative. Redis loss delays delivery, while polling and outbox reconciliation recover durable queued work.
- Cancellation of queued work is immediate. Running work becomes `CANCEL_REQUESTED` until the worker confirms termination or its lease expires.

Required chaos measurements include detection delay, lease-expiry delay, requeue latency, recovery time, tail latency, and lost-job count. Two harnesses produce them: the load generator's `--kill-fraction` (worker loss with a PostgreSQL audit, `benchmarks/run_native.py`) and `tests/chaos/run_scenarios.py`, which adds scheduler, PostgreSQL, and Redis restarts and computes every figure from PostgreSQL timestamps. Measured results are in `benchmarks/reports`.

Detection is bounded by lease expiry, not by stream closure: the gateway does not act on a dropped gRPC stream, so a dead worker stays "healthy" until its last heartbeat is 15 s old and keeps receiving offers until then; each of those offers expires after its 10 s acknowledgement deadline and is requeued. `agent-fabric-loadgen --kill-fraction` produces them from PostgreSQL timestamps; measured values live in `benchmarks/reports`.

