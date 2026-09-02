# Architecture

Agent Fabric separates durable intent from transient delivery. FastAPI validates a run and commits both the run and an outbox record in one PostgreSQL transaction. A dedicated outbox publisher process (`agent-fabric-outbox`) copies delivery notifications to Redis Streams in pipelined batches. It originally ran inside the API process; the first benchmark showed a submission burst starving it for 20-43 s, long enough for lease offers to expire before delivery, so it became its own process (see `benchmarks/reports`). The scheduler always re-reads and locks PostgreSQL state, refreshing the locked rows, before reserving resources, so a duplicate or stale Redis message cannot create a second active lease and a concurrent release cannot be overwritten.

Workers initiate bidirectional gRPC streams through HAProxy to eight gateway
processes. This avoids inbound worker ports and carries registration,
heartbeats, lease acknowledgements, ordered events, completions, cancellation,
and cleanup confirmation. Each gateway publishes worker ownership in Redis,
consumes one shard-level outbound stream, and fans messages into its in-memory
connection registry. A supervised reader reconnects and recreates its consumer
group after Redis interruption. The scheduler remains a single active process.

Run state and attempt state are separate. A run may have several attempts, but only one live lease. Every state-changing worker message includes an attempt identifier and opaque lease token. Only the token hash is stored.

Logs are immutable objects in MinIO, with sequence and object indexes in PostgreSQL. Local MinIO maps directly to a future S3 implementation without putting high-volume payloads into the transactional database.

