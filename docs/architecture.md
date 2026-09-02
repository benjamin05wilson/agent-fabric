# Architecture

The system separates durable control-plane state from transient delivery. The
diagram in the [README](../README.md#architecture) is the quickest component
map; this document describes the consistency and scaling boundaries behind it.

## Durable control plane

FastAPI validates a run and commits the run plus an outbox record in one
PostgreSQL transaction. PostgreSQL is authoritative for projects, runs,
attempts, leases, worker inventory, and resource reservations. Redis Streams
carry at-least-once delivery messages, but no correctness decision depends on
Redis being the source of truth.

A dedicated `agent-fabric-outbox` process locks unpublished outbox rows with
`FOR UPDATE SKIP LOCKED`, resolves the gateway that owns each target worker,
and publishes a batch to Redis. Separating this process from FastAPI fixed the
measured 20–43 second delivery starvation caused by large submission bursts.

## Parallel scheduling plane

One or more scheduler processes poll durable queued state. Replicas claim
disjoint candidate batches with `FOR UPDATE SKIP LOCKED`, plan against
independently seeded rotating windows of healthy workers, and keep the
CPU-heavy planning phase parallel. A short, non-blocking PostgreSQL transaction
advisory lock protects the two global commit invariants: tenant running limits
and the maximum number of outstanding lease offers.

The winning replica rechecks global counts, locks only the selected worker rows
in stable order, and revalidates CPU, memory, PID, GPU, VRAM, capability, gVisor,
health, and drain constraints. It then commits attempt creation, run state,
relative reservations, and lease-offer outbox rows atomically. A losing replica
releases its candidate locks and retries on the next poll rather than waiting
while holding rows needed by acknowledgement traffic.

This design is correctness-clean through four measured scheduler replicas, but
throughput peaked at two. Eight replicas created expired unacknowledged offers,
so the repository does not claim linear scheduler scaling.

## Sharded connection plane

Workers initiate long-lived bidirectional gRPC streams through HAProxy to eight
gateway processes. No inbound worker port is required. Each gateway owns a
local connection registry, records its worker ownership in Redis, and consumes
one shard-level outbound stream rather than creating one Redis reader per
worker. Lease offers and cancellations are routed to the owning shard and then
fanned out in memory.

The reverse stream carries registration, coalesced heartbeats, lease
acknowledgements, ordered events, completion, cancellation state, and cleanup
confirmation. Heartbeats are flushed in bounded database batches, and run
events pass through a bounded queue with dedicated persistence workers. The
Redis reader is supervised, resumes pending entries, and recreates its consumer
group after an interruption.

## Lease and recovery model

Run state and attempt state are separate. A run may have multiple attempts but
only one live lease. Every state-changing worker message includes an attempt ID
and opaque lease token; PostgreSQL stores only the token hash. Expired offers
that were never acknowledged can be safely requeued. An acknowledged lease is
retried only when the run explicitly declares retry safety, because execution
may have occurred before the worker disappeared.

Duplicate outbox publication and duplicate worker messages are expected under
at-least-once delivery. Transactional state checks and attempt identities make
them idempotent. Reconciliation releases reservations and reconstructs queued
work from PostgreSQL after scheduler, gateway, Redis, or worker interruption.

## Logs and observability

Run logs are immutable objects in MinIO, with sequence and object indexes in
PostgreSQL. The same interface targets S3 in the Terraform environment without
putting high-volume payloads in the transactional database. OpenTelemetry,
Prometheus, Tempo, Grafana, structured logs, and benchmark audits cover the API,
schedulers, gateways, outbox, workers, queues, leases, and reservations.

## Verified boundary

The frozen portfolio baseline is 50,000 simulated workers represented by
50,000 durable PostgreSQL rows and 50,000 simultaneous real gRPC streams on the
documented Docker Desktop host. The failed 100,000-worker attempt is retained
as boundary evidence, not a capability claim. Any higher-tier work should move
to a properly tuned Linux host first.
