#!/usr/bin/env bash
# Runs the chaos scenarios against a natively running stack (see docs/failures.md).
# Restart commands are host-specific; override them through the AF_* variables.
set -euo pipefail
export AF_PGURL=${AF_PGURL:-postgresql://agent_fabric:agent_fabric@127.0.0.1:5432/agent_fabric}
export AF_RESET=${AF_RESET:-"psql $AF_PGURL -Atq -c 'TRUNCATE run_event_indexes, attempts, outbox_events, runs, workers;'"}
export AF_SCHEDULER_RESTART=${AF_SCHEDULER_RESTART:-""}
export AF_POSTGRES_RESTART=${AF_POSTGRES_RESTART:-""}
export AF_REDIS_RESTART=${AF_REDIS_RESTART:-""}
export GIT_REVISION=${GIT_REVISION:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}
exec python tests/chaos/run_scenarios.py --output "${AF_RESULTS:-benchmarks/results/chaos}" "$@"
