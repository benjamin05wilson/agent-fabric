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
