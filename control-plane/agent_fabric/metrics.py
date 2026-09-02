from prometheus_client import Counter, Gauge, Histogram

API_REQUESTS = Counter(
    "agent_fabric_api_requests_total", "API requests", ["method", "route", "status"]
)
RUNS_CREATED = Counter("agent_fabric_runs_created_total", "Runs accepted", ["project"])
RUN_TRANSITIONS = Counter(
    "agent_fabric_run_transitions_total", "Run state transitions", ["from_state", "to_state"]
)
QUEUE_DEPTH = Gauge("agent_fabric_queue_depth", "Runs waiting for placement")
HEALTHY_WORKERS = Gauge("agent_fabric_healthy_workers", "Workers within heartbeat threshold")
SCHEDULING_SECONDS = Histogram(
    "agent_fabric_scheduling_seconds",
    "Time spent selecting and reserving a worker",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
LEASE_EXPIRATIONS = Counter(
    "agent_fabric_lease_expirations_total", "Expired leases", ["acknowledged", "outcome"]
)
PLACEMENTS = Counter("agent_fabric_placements_total", "Runs leased to workers")
OUTSTANDING_OFFERS = Gauge(
    "agent_fabric_outstanding_offers", "Lease offers not yet acknowledged by a worker"
)
WORKER_REGISTRATIONS = Counter(
    "agent_fabric_worker_registrations_total", "Worker stream registrations"
)
ACTIVE_WORKER_STREAMS = Gauge(
    "agent_fabric_active_worker_streams", "Currently connected worker streams"
)
HEARTBEATS = Counter("agent_fabric_heartbeats_total", "Worker heartbeats processed")
GATEWAY_MESSAGE_SECONDS = Histogram(
    "agent_fabric_gateway_message_seconds",
    "Gateway processing time by worker message kind",
    ["kind"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)
LEASES_DELIVERED = Counter(
    "agent_fabric_leases_delivered_total", "Lease offers placed on worker streams"
)
LEASE_ACKNOWLEDGEMENTS = Counter(
    "agent_fabric_lease_acknowledgements_total", "Lease acknowledgements processed", ["accepted"]
)
GATEWAY_OUTBOUND_MESSAGES = Counter(
    "agent_fabric_gateway_outbound_messages_total",
    "Shard-routed messages placed on worker streams",
    ["kind"],
)
GATEWAY_READER_RESTARTS = Counter(
    "agent_fabric_gateway_reader_restarts_total",
    "Outbound Redis reader restarts after an error",
)
GATEWAY_LOCAL_CONNECTIONS = Gauge(
    "agent_fabric_gateway_local_connections",
    "Worker connections owned by this gateway shard",
)
GATEWAY_EVENT_QUEUE_DEPTH = Gauge(
    "agent_fabric_gateway_event_queue_depth",
    "Bulk worker events waiting for persistence on this gateway shard",
)
