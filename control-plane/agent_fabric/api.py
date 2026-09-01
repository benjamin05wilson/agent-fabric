import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from opentelemetry import propagate
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import authenticated_project, seed_development_project
from .config import get_settings
from .db import engine, get_session, session_factory
from .log_store import log_store
from .metrics import API_REQUESTS, RUNS_CREATED
from .models import (
    TERMINAL_STATES,
    Base,
    OutboxEvent,
    Project,
    Run,
    RunEventIndex,
    RunState,
    Worker,
)
from .schemas import LogPage, LogRecord, RunAccepted, RunCreate, RunResponse, WorkerResponse
from .telemetry import configure_telemetry

logger = logging.getLogger(__name__)


def _problem(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": f"https://agent-fabric.local/problems/{code}",
            "title": code.replace("_", " ").title(),
            "status": status_code,
            "detail": detail,
            "code": code,
        },
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        await seed_development_project(session)
    await log_store.ensure_bucket()
    # The outbox publisher runs as its own process (agent-fabric-outbox); hosting it
    # here let a submission burst starve it and expire lease offers before delivery.
    yield


configure_telemetry("agent-fabric-api")
app = FastAPI(title="Agent Fabric", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    route = request.scope.get("route")
    route_path = getattr(route, "path", "unmatched")
    API_REQUESTS.labels(request.method, route_path, response.status_code).inc()
    return cast(Response, response)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    code = "request_rejected" if exc.status_code < 500 else "internal_error"
    return _problem(exc.status_code, code, str(exc.detail))


def _serialize_run(run: Run) -> RunResponse:
    attempts = [
        {
            "id": str(attempt.id),
            "number": attempt.number,
            "worker_id": attempt.worker_id,
            "state": attempt.state.value,
            "acknowledged_at": attempt.acknowledged_at,
            "finished_at": attempt.finished_at,
        }
        for attempt in sorted(run.attempts, key=lambda item: item.number)
    ]
    failure = None
    if run.failure_code:
        failure = {"code": run.failure_code, "message": run.failure_message or ""}
    return RunResponse(
        id=str(run.id),
        state=run.state,
        specification={key: value for key, value in run.spec.items() if not key.startswith("_")},
        attempts=attempts,  # type: ignore[arg-type]
        result=run.result,
        failure=failure,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@app.post("/runs", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: RunCreate,
    project: Annotated[Project, Depends(authenticated_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> RunAccepted | JSONResponse:
    canonical = body.model_dump_json(exclude_none=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    existing = await session.scalar(
        select(Run).where(
            Run.project_id == project.id,
            Run.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            return _problem(409, "idempotency_conflict", "key was used with a different request")
        return RunAccepted(
            id=str(existing.id), state=existing.state, created_at=existing.created_at
        )

    queued = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.project_id == project.id, Run.state == RunState.QUEUED)
    )
    if (queued or 0) >= project.max_queued:
        return _problem(429, "project_queue_full", "project admission limit reached")

    specification = body.model_dump(mode="json")
    trace_carrier: dict[str, str] = {}
    propagate.inject(trace_carrier)
    specification["_trace_context"] = trace_carrier
    run = Run(
        project_id=project.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        spec=specification,
        priority=body.priority,
        retry_safe=body.retry.safe_on_worker_loss,
        max_attempts=body.retry.max_attempts,
    )
    session.add(run)
    try:
        await session.flush()
        session.add(
            OutboxEvent(
                topic="run.ready",
                aggregate_id=str(run.id),
                payload={"run_id": str(run.id), "project_id": str(project.id)},
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Run).where(Run.project_id == project.id, Run.idempotency_key == idempotency_key)
        )
        if existing and existing.request_hash == request_hash:
            return RunAccepted(
                id=str(existing.id), state=existing.state, created_at=existing.created_at
            )
        return _problem(409, "idempotency_conflict", "key was used concurrently")
    RUNS_CREATED.labels(project.slug).inc()
    return RunAccepted(id=str(run.id), state=run.state, created_at=run.created_at)


@app.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    project: Annotated[Project, Depends(authenticated_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunResponse:
    run = await session.scalar(select(Run).where(Run.id == run_id, Run.project_id == project.id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize_run(run)


@app.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: uuid.UUID,
    project: Annotated[Project, Depends(authenticated_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    async with session.begin():
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.project_id == project.id).with_for_update()
        )
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.state not in TERMINAL_STATES and run.state != RunState.CANCEL_REQUESTED:
            if run.state == RunState.QUEUED:
                run.state = RunState.CANCELLED
                run.finished_at = datetime.now(UTC)
            else:
                run.state = RunState.CANCEL_REQUESTED
                session.add(
                    OutboxEvent(
                        topic="run.cancel",
                        aggregate_id=str(run.id),
                        payload={"run_id": str(run.id)},
                    )
                )
    return {"id": str(run.id), "state": run.state.value}


@app.get("/runs/{run_id}/logs", response_model=LogPage)
async def get_logs(
    run_id: uuid.UUID,
    project: Annotated[Project, Depends(authenticated_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> LogPage:
    exists = await session.scalar(
        select(Run.id).where(Run.id == run_id, Run.project_id == project.id)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="run not found")
    indexes = (
        await session.scalars(
            select(RunEventIndex)
            .where(RunEventIndex.run_id == run_id, RunEventIndex.id > after)
            .order_by(RunEventIndex.id)
            .limit(limit)
        )
    ).all()
    records: list[LogRecord] = []
    for index in indexes:
        data = (await log_store.get(index.object_key)).decode(errors="replace")
        records.append(
            LogRecord(
                cursor=index.id,
                attempt_id=str(index.attempt_id),
                sequence=index.sequence,
                stream=index.stream,
                data=data,
                created_at=index.created_at,
            )
        )
    return LogPage(records=records, next_cursor=records[-1].cursor if records else None)


@app.get("/workers", response_model=list[WorkerResponse])
async def get_workers(
    _: Annotated[Project, Depends(authenticated_project)],
    session: Annotated[AsyncSession, Depends(get_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[WorkerResponse]:
    workers = (
        await session.scalars(select(Worker).order_by(Worker.id).offset(offset).limit(limit))
    ).all()
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().unhealthy_after_seconds)
    return [
        WorkerResponse(
            id=worker.id,
            version=worker.worker_version,
            healthy=worker.last_seen_at >= cutoff,
            draining=worker.draining,
            capacity={
                "cpu_millis": worker.cpu_millis,
                "memory_mb": worker.memory_mb,
                "pids": worker.pids,
            },
            reserved={
                "cpu_millis": worker.reserved_cpu_millis,
                "memory_mb": worker.reserved_memory_mb,
                "pids": worker.reserved_pids,
            },
            capabilities=worker.capabilities,
            sandbox_backends=worker.sandbox_backends,
            last_seen_at=worker.last_seen_at,
        )
        for worker in workers
    ]


@app.get("/health", response_model=None)
async def health() -> dict[str, Any] | JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = type(exc).__name__
    redis = Redis.from_url(get_settings().redis_url)
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = type(exc).__name__
    finally:
        await redis.aclose()
    if any(value != "ok" for value in checks.values()):
        return _problem(503, "dependency_unavailable", json.dumps(checks))
    return {"status": "ok", "checks": checks}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def run() -> None:
    uvicorn.run("agent_fabric.api:app", host="0.0.0.0", port=8000)
