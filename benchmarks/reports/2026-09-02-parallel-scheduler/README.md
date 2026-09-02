# Parallel scheduler experiment — 2026-09-02

## Outcome

Parallel scheduling is implemented, but the 50,000-worker 1/2/4/8 acceptance
matrix is **not claimed**.

The exact 50,000-stream, 10,000-job single-scheduler control completed in
181.254 seconds at 50.98 measured placements/s with exactly 10,000 successful
attempts, zero retries, zero lost runs, zero unpublished outbox events, and zero
reservation leak. This validated rotating bounded worker scans and targeted
commit-time capacity locks without changing the proven gateway topology.

The queued-work protocol then stopped schedulers, prefilled durable runs, and
timed only the drain. A 1,000-job smoke test recorded 39.073 effective
placements/s with one scheduler. A later two-scheduler diagnostic completed all
1,000 runs in 42.781 seconds but needed six retries of unacknowledged offers; no
run was lost and reservations returned to zero. Wider offer-window experiments
increased these delivery failures and were reverted.

Those results are not evidence of scheduler speedup. They show that the safe
global 100-offer backpressure window and a small population of one-way-stalled
simulated streams dominate before scheduler replicas can demonstrate scaling.
The repository therefore keeps the safe bound and does not publish projected
2/4/8 numbers.

## Implemented concurrency model

- Queued runs are claimed with `FOR UPDATE SKIP LOCKED`, so scheduler processes
  receive disjoint candidate batches.
- Worker planning uses independently seeded, rotating 5,000-worker keyset
  windows rather than repeatedly loading all 50,000 rows.
- Equal-capacity workers are shuffled and each batch spreads offers before
  assigning a second offer to one stream; GPU workers remain reserved for GPU
  work when CPU-only capacity exists.
- The commit phase takes a transaction-scoped PostgreSQL advisory lock, rechecks
  the global outstanding-offer ceiling and exact tenant running counts, then
  locks only selected worker rows.
- CPU, memory, PID, GPU, and VRAM reservations are revalidated and mutated on the
  locked rows before a batched flush.
- The acknowledgement deadline is stamped immediately before persistence, not
  before CPU-heavy planning.
- Compose exposes one baseline scheduler by default and seven opt-in replicas in
  the `scheduler-scale` profile. Prometheus aggregates all replica metrics.

## Correctness checks

The unit suite covers capacity overcommit rejection, tenant/global commit
trimming, GPU capability and VRAM constraints, and preservation of GPU workers
for accelerator jobs. The benchmark audit records total retry attempts, expired
unacknowledged offers, duplicate acknowledged executions, terminal states,
unpublished outbox events, and every reservation dimension.

Static validation at this revision: Ruff clean, strict mypy clean, 27 unit tests
passing, and the scheduler-scale Compose profile renders successfully.

## Next experiment

Before rerunning the full matrix, make outbound readiness observable or isolate
the high-density Python fleet simulator from the control plane. The acceptance
gate remains: 50,000 simultaneous durable/live streams, 10,000 jobs per replica
count, zero duplicate acknowledged executions, zero reservation leaks, and all
runs terminal. Retry offers must be reported separately rather than hidden.
