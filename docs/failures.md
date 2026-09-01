# Failure semantics

Delivery is at least once. Effects are not claimed to be exactly once.

- An unacknowledged expired offer is safely requeued.
- An acknowledged attempt is considered uncertain when heartbeats stop.
- Uncertain execution is retried only when the caller explicitly declares it safe and has remaining attempts; otherwise the run becomes `LOST`.
- Repeated acknowledgements, events, completions, cleanup confirmations, API submissions, and cancellations are idempotent.
- PostgreSQL is authoritative. Redis loss delays delivery, while polling and outbox reconciliation recover durable queued work.
- Cancellation of queued work is immediate. Running work becomes `CANCEL_REQUESTED` until the worker confirms termination or its lease expires.

Required chaos measurements include detection delay, lease-expiry delay, requeue latency, recovery time, tail latency, and lost-job count.

