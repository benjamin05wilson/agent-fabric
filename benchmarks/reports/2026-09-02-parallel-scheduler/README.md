# Parallel scheduler experiment — 2026-09-02

## Outcome

Parallel scheduling produces a measured clean gain through two replicas, but
the complete 50,000-worker 1/2/4/8 acceptance matrix is **not claimed** because
eight replicas fail the zero-retry gate.

The queued-work protocol stopped all schedulers, prefilled 10,000 durable runs,
then timed only the drain against 50,000 simultaneously durable, heartbeating,
active gRPC worker streams. The accepted 100-offer bound and rotating
5,000-worker window were unchanged between phases.

| Schedulers | Drain seconds | Effective jobs/s | Attempts | Unacknowledged retries | Duplicate acknowledged executions | Reservation leak |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 148.859 | 67.178 | 10,000 | 0 | 0 | 0 |
| 2 | 108.734 | 91.968 | 10,000 | 0 | 0 | 0 |
| 4 | 120.797 | 82.784 | 10,000 | 0 | 0 | 0 |
| 8 | 157.170 | 63.620 | 10,062 | 62 | 0 | 0 |

Two replicas improve clean drain throughput by 36.9% over one. Four replicas
remain correctness-clean and 23.2% above one replica, but regress from the
two-replica peak. Eight replicas are both slower than one and generate 62
expired unacknowledged offers. No phase loses a final run or leaks a CPU,
memory, PID, GPU, or VRAM reservation.

An interrupted four-replica run was excluded: its timestamps contain a 3h35m
host suspension, after which two acknowledged leases correctly expired and
were retried. The table uses a fresh uninterrupted 4/8 rerun.

## Implemented concurrency model

- Queued runs are claimed with `FOR UPDATE SKIP LOCKED`, so scheduler processes
  receive disjoint candidate batches.
- Worker planning uses independently seeded, rotating 5,000-worker keyset
  windows rather than repeatedly loading all 50,000 rows.
- Equal-capacity workers are shuffled and each batch spreads offers before
  assigning a second offer to one stream; GPU workers remain reserved for GPU
  work when CPU-only capacity exists.
- The commit phase tries a transaction-scoped PostgreSQL advisory lock, rechecks
  the global outstanding-offer ceiling and exact tenant running counts, then
  locks only selected worker rows. A losing replica releases its candidate rows
  immediately instead of waiting while holding them; this removed the measured
  Run/advisory/Worker acknowledgement deadlock cycle.
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

The benchmark now clears Redis delivery state with PostgreSQL workload state
before attaching a new fleet, fails immediately on any correctness violation,
and gives all scheduler services one explicit image tag so a sweep cannot mix
old and new replica images.

Static validation at this revision: Ruff clean, strict mypy clean, 27 unit tests
passing, and the scheduler-scale Compose profile renders successfully.

## Next experiment

The next scheduler change should remove the wasted planning/commit contention
above two replicas while keeping the exact global offer, tenant, capacity, and
reservation invariants. Repeat this exact matrix afterward. The acceptance gate
remains zero retries, zero duplicate acknowledged executions, zero reservation
leaks, and all 10,000 runs terminal at every replica count.
