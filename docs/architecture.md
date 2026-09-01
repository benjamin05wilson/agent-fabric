# Architecture

Agent Fabric separates durable intent from transient delivery. FastAPI validates a run and commits both the run and an outbox record in one PostgreSQL transaction. The outbox publisher copies delivery notifications to Redis Streams. The scheduler always re-reads and locks PostgreSQL state before reserving resources, so a duplicate or stale Redis message cannot create a second active lease.

Workers initiate bidirectional gRPC streams. This avoids inbound worker ports and carries registration, heartbeats, lease acknowledgements, ordered events, completions, cancellation, and cleanup confirmation. The baseline intentionally uses one scheduler and one gateway; these are restart-safe but not yet horizontally sharded.

Run state and attempt state are separate. A run may have several attempts, but only one live lease. Every state-changing worker message includes an attempt identifier and opaque lease token. Only the token hash is stored.

Logs are immutable objects in MinIO, with sequence and object indexes in PostgreSQL. Local MinIO maps directly to a future S3 implementation without putting high-volume payloads into the transactional database.

