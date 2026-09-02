import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import grpc
from opentelemetry.instrumentation.grpc import aio_server_interceptor
from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select, update

from .config import get_settings
from .db import session_factory
from .generated import worker_pb2, worker_pb2_grpc
from .log_store import log_store
from .metrics import (
    ACTIVE_WORKER_STREAMS,
    GATEWAY_EVENT_QUEUE_DEPTH,
    GATEWAY_LOCAL_CONNECTIONS,
    GATEWAY_MESSAGE_SECONDS,
    GATEWAY_OUTBOUND_MESSAGES,
    GATEWAY_READER_RESTARTS,
    HEARTBEATS,
    LEASE_ACKNOWLEDGEMENTS,
    LEASES_DELIVERED,
    WORKER_REGISTRATIONS,
)
from .models import (
    Attempt,
    AttemptState,
    OutboxEvent,
    Run,
    RunEventIndex,
    RunState,
    Worker,
    utcnow,
)
from .telemetry import configure_telemetry

logger = logging.getLogger(__name__)


def _valid_token(attempt: Attempt, raw_token: str) -> bool:
    supplied = hashlib.sha256(raw_token.encode()).hexdigest()
    return hmac.compare_digest(attempt.lease_token_hash, supplied)


def _release(worker: Worker, run: Run) -> None:
    resources = run.spec["resources"]
    worker.reserved_cpu_millis = max(0, worker.reserved_cpu_millis - resources["cpu_millis"])
    worker.reserved_memory_mb = max(0, worker.reserved_memory_mb - resources["memory_mb"])
    worker.reserved_pids = max(0, worker.reserved_pids - resources["pids"])
    worker.reserved_gpu_count = max(0, worker.reserved_gpu_count - resources.get("gpu", 0))
    worker.reserved_vram_mb = max(0, worker.reserved_vram_mb - resources.get("vram_mb", 0))


class WorkerControlService(worker_pb2_grpc.WorkerControlServicer):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.gateway_id = self.settings.gateway_id
        self.connections: dict[
            str, asyncio.Queue[worker_pb2.ControlMessage | None]
        ] = {}
        self.pending_heartbeats: dict[str, tuple[datetime, list[uuid.UUID], list[str]]] = {}
        self.event_queue: asyncio.Queue[tuple[str, worker_pb2.RunEvent]] = asyncio.Queue(
            maxsize=self.settings.gateway_event_queue_size
        )
        self.event_writers: list[asyncio.Task[None]] = []
        self.heartbeat_flusher: asyncio.Task[None] | None = None
        self.outbound_reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.heartbeat_flusher = asyncio.create_task(
            self._heartbeat_flush_loop(), name="heartbeat-flusher"
        )
        self.outbound_reader = asyncio.create_task(
            self._outbound_supervisor(), name="outbound-reader"
        )
        self.event_writers = [
            asyncio.create_task(self._event_writer(), name=f"event-writer-{index}")
            for index in range(self.settings.gateway_event_workers)
        ]

    async def Connect(
        self,
        request_iterator: AsyncIterator[worker_pb2.WorkerMessage],
        context: grpc.aio.ServicerContext[worker_pb2.WorkerMessage, worker_pb2.ControlMessage],
    ) -> AsyncIterator[worker_pb2.ControlMessage]:
        outgoing: asyncio.Queue[worker_pb2.ControlMessage | None] = asyncio.Queue(maxsize=1000)
        worker_id: str | None = None
        ACTIVE_WORKER_STREAMS.inc()

        async def receive() -> None:
            nonlocal worker_id
            try:
                async for message in request_iterator:
                    if not worker_id:
                        if message.WhichOneof("payload") != "register" or not message.worker_id:
                            await context.abort(
                                grpc.StatusCode.FAILED_PRECONDITION, "register first"
                            )
                            return
                        worker_id = message.worker_id
                        started = time.perf_counter()
                        await self._register(worker_id, message.register)
                        await self._attach(worker_id, outgoing)
                        GATEWAY_MESSAGE_SECONDS.labels("register").observe(
                            time.perf_counter() - started
                        )
                        WORKER_REGISTRATIONS.inc()
                        continue
                    if message.worker_id != worker_id:
                        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker id changed")
                        return
                    kind = message.WhichOneof("payload")
                    started = time.perf_counter()
                    if kind == "heartbeat":
                        await self._heartbeat(worker_id, message.heartbeat)
                        HEARTBEATS.inc()
                    elif kind == "acknowledgement":
                        await self._acknowledge(worker_id, message.acknowledgement)
                        LEASE_ACKNOWLEDGEMENTS.labels(
                            str(message.acknowledgement.accepted).lower()
                        ).inc()
                    elif kind == "event":
                        copied_event = worker_pb2.RunEvent()
                        copied_event.CopyFrom(message.event)
                        await self.event_queue.put((worker_id, copied_event))
                        GATEWAY_EVENT_QUEUE_DEPTH.set(self.event_queue.qsize())
                    elif kind == "completion":
                        await self._complete(worker_id, message.completion)
                    elif kind == "cleanup":
                        await self._cleanup(worker_id, message.cleanup)
                    GATEWAY_MESSAGE_SECONDS.labels(kind or "unknown").observe(
                        time.perf_counter() - started
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker stream failed", extra={"worker_id": worker_id})
            finally:
                await outgoing.put(None)

        receiver = asyncio.create_task(receive(), name="worker-receiver")
        try:
            while True:
                response = await outgoing.get()
                if response is None:
                    break
                yield response
        finally:
            ACTIVE_WORKER_STREAMS.dec()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            if worker_id is not None:
                await self._detach(worker_id, outgoing)

    async def _register(self, worker_id: str, message: Any) -> None:
        if message.protocol_version != "v1":
            raise ValueError("unsupported worker protocol")
        async with session_factory() as session, session.begin():
            worker = await session.get(Worker, worker_id)
            values = {
                "protocol_version": message.protocol_version,
                "worker_version": message.worker_version,
                "cpu_millis": message.cpu_millis,
                "memory_mb": message.memory_mb,
                "pids": message.pids,
                "gpu_count": message.gpu_count,
                "vram_mb": message.vram_mb,
                "capabilities": list(message.capabilities),
                "sandbox_backends": list(message.sandbox_backends),
                "last_seen_at": utcnow(),
            }
            if worker is None:
                session.add(Worker(id=worker_id, **values))
            else:
                for key, value in values.items():
                    setattr(worker, key, value)

    async def _heartbeat(self, worker_id: str, message: Any) -> None:
        now = utcnow()
        active_ids: list[uuid.UUID] = []
        for raw_id in message.active_attempt_ids:
            try:
                active_ids.append(uuid.UUID(raw_id))
            except ValueError:
                continue
        # Coalesce heartbeats in memory. A fleet of 10,000 workers produces 2,000
        # heartbeats/s; one PostgreSQL transaction plus one Redis command per heartbeat
        # saturated the gateway at roughly 4,000 workers. The flusher persists the latest
        # heartbeat for every worker in bounded batches.
        self.pending_heartbeats[worker_id] = (
            now,
            active_ids,
            list(message.active_attempt_ids),
        )

    async def _heartbeat_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self._flush_heartbeats()

    async def _attach(
        self,
        worker_id: str,
        outgoing: asyncio.Queue[worker_pb2.ControlMessage | None],
    ) -> None:
        previous = self.connections.get(worker_id)
        self.connections[worker_id] = outgoing
        GATEWAY_LOCAL_CONNECTIONS.set(len(self.connections))
        await self.redis.hset("af:worker:owners", worker_id, self.gateway_id)
        if previous is not None and previous is not outgoing:
            try:
                previous.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _detach(
        self,
        worker_id: str,
        outgoing: asyncio.Queue[worker_pb2.ControlMessage | None],
    ) -> None:
        if self.connections.get(worker_id) is not outgoing:
            return
        del self.connections[worker_id]
        GATEWAY_LOCAL_CONNECTIONS.set(len(self.connections))
        # Keep the last owner as a durable routing hint. Messages arriving during
        # reconnect remain pending on this shard and are forwarded if ownership moves.

    async def _outbound_supervisor(self) -> None:
        backoff = 0.1
        while True:
            try:
                await self._outbound_loop()
                backoff = 0.1
            except asyncio.CancelledError:
                raise
            except ResponseError as error:
                GATEWAY_READER_RESTARTS.inc()
                if "NOGROUP" in str(error):
                    # Benchmarks deliberately FLUSHDB between tiers. Recreate the
                    # consumer group without logging an alarming traceback.
                    logger.info(
                        "gateway outbound consumer group was reset",
                        extra={"gateway_id": self.gateway_id},
                    )
                else:
                    logger.exception(
                        "gateway outbound reader failed",
                        extra={"gateway_id": self.gateway_id},
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            except Exception:
                GATEWAY_READER_RESTARTS.inc()
                logger.exception(
                    "gateway outbound reader failed", extra={"gateway_id": self.gateway_id}
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _outbound_loop(self) -> None:
        stream = f"af:gateway:{self.gateway_id}:outbound"
        group = "gateway"
        try:
            await self.redis.xgroup_create(stream, group, id="0-0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

        while True:
            pending = await self.redis.xreadgroup(
                group, self.gateway_id, {stream: "0"}, count=100
            )
            has_pending = any(entries for _, entries in pending)
            messages = (
                pending
                if has_pending
                else await self.redis.xreadgroup(
                    group, self.gateway_id, {stream: ">"}, count=100, block=1000
                )
            )
            if not messages:
                continue
            delivered = 0
            for _, entries in messages:
                for entry_id, fields in entries:
                    if await self._deliver_outbound(stream, group, entry_id, fields):
                        delivered += 1
            if has_pending and delivered == 0:
                # Pending work can be waiting for a worker to reconnect. Avoid turning
                # that durable retry into a busy loop.
                await asyncio.sleep(0.1)

    async def _deliver_outbound(
        self,
        stream: str,
        group: str,
        entry_id: str,
        fields: dict[str, str],
    ) -> bool:
        worker_id = fields["worker_id"]
        outgoing = self.connections.get(worker_id)
        if outgoing is None:
            owner = await self.redis.hget("af:worker:owners", worker_id)
            if owner and owner != self.gateway_id:
                await self.redis.xadd(
                    f"af:gateway:{owner}:outbound", fields, maxlen=100_000, approximate=True
                )
                await self._finish_outbound(stream, group, entry_id)
                return True
            return False
        payload = json.loads(fields["payload"])
        kind = fields["kind"]
        if kind == "lease":
            message = worker_pb2.ControlMessage(lease=worker_pb2.LeaseOffer(**payload))
            LEASES_DELIVERED.inc()
        elif kind == "cancel":
            message = worker_pb2.ControlMessage(
                cancel=worker_pb2.CancelRun(
                    run_id=payload["run_id"], attempt_id=payload["attempt_id"]
                )
            )
        else:
            logger.error("unknown outbound message", extra={"kind": kind})
            await self._finish_outbound(stream, group, entry_id)
            return True
        try:
            outgoing.put_nowait(message)
        except asyncio.QueueFull:
            return False
        GATEWAY_OUTBOUND_MESSAGES.labels(kind).inc()
        await self._finish_outbound(stream, group, entry_id)
        return True

    async def _finish_outbound(self, stream: str, group: str, entry_id: str) -> None:
        pipeline = self.redis.pipeline(transaction=False)
        pipeline.xack(stream, group, entry_id)
        pipeline.xdel(stream, entry_id)
        await pipeline.execute()

    async def _flush_heartbeats(self) -> int:
        pending, self.pending_heartbeats = self.pending_heartbeats, {}
        if not pending:
            return 0
        now = utcnow()
        expiry = now + timedelta(seconds=self.settings.unhealthy_after_seconds)
        active_ids = sorted(
            {attempt_id for _, ids, _ in pending.values() for attempt_id in ids}, key=str
        )
        try:
            # Worker liveness and attempt renewal intentionally use separate transactions.
            # Completion locks Attempt -> Run -> Worker; holding Worker while bulk-locking
            # Attempt rows creates the inverse order and can deadlock under load.
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(Worker).where(Worker.id.in_(pending)).values(last_seen_at=now)
                )

            # Lock renewals in a deterministic order and skip attempts currently being
            # completed. A completing attempt no longer needs its lease extended.
            for offset in range(0, len(active_ids), 500):
                chunk = active_ids[offset : offset + 500]
                async with session_factory() as session, session.begin():
                    locked_attempts = (
                        select(Attempt.id)
                        .where(
                            Attempt.id.in_(chunk),
                            Attempt.state == AttemptState.RUNNING,
                        )
                        .order_by(Attempt.id)
                        .with_for_update(skip_locked=True)
                        .cte("heartbeat_attempts")
                    )
                    await session.execute(
                        update(Attempt)
                        .where(Attempt.id.in_(select(locked_attempts.c.id)))
                        .values(lease_expires_at=expiry)
                    )

            await self.redis.hset(
                "af:worker:liveness",
                mapping={
                    worker_id: json.dumps(
                        {"seen": seen.timestamp(), "active": active}, separators=(",", ":")
                    )
                    for worker_id, (seen, _, active) in pending.items()
                },
            )
        except Exception:
            for worker_id, heartbeat in pending.items():
                self.pending_heartbeats.setdefault(worker_id, heartbeat)
            logger.exception("heartbeat batch failed", extra={"worker_count": len(pending)})
            return 0
        return len(pending)

    async def _event_writer(self) -> None:
        while True:
            worker_id, event = await self.event_queue.get()
            try:
                started = time.perf_counter()
                await self._event(worker_id, event)
                GATEWAY_MESSAGE_SECONDS.labels("event_persist").observe(
                    time.perf_counter() - started
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "worker event persistence failed",
                    extra={"worker_id": worker_id, "attempt_id": event.attempt_id},
                )
            finally:
                self.event_queue.task_done()
                GATEWAY_EVENT_QUEUE_DEPTH.set(self.event_queue.qsize())

    async def close(self) -> None:
        background = [
            task
            for task in (self.heartbeat_flusher, self.outbound_reader)
            if task is not None
        ]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        try:
            await asyncio.wait_for(self.event_queue.join(), timeout=5.0)
        except TimeoutError:
            logger.warning(
                "timed out draining gateway event queue",
                extra={"queued_events": self.event_queue.qsize()},
            )
        for task in self.event_writers:
            task.cancel()
        await asyncio.gather(*self.event_writers, return_exceptions=True)
        await self._flush_heartbeats()
        await self.redis.aclose()

    async def _acknowledge(self, worker_id: str, message: Any) -> None:
        attempt_id = uuid.UUID(message.attempt_id)
        async with session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == attempt_id).with_for_update()
            )
            if (
                attempt is None
                or attempt.worker_id != worker_id
                or not _valid_token(attempt, message.lease_token)
            ):
                raise ValueError("invalid lease acknowledgement")
            if attempt.state != AttemptState.OFFERED:
                return
            run = await session.scalar(
                select(Run).where(Run.id == attempt.run_id).with_for_update()
            )
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if run is None or worker is None:
                raise ValueError("lease references missing state")
            if message.accepted:
                attempt.state = AttemptState.RUNNING
                attempt.acknowledged_at = utcnow()
                attempt.lease_expires_at = utcnow() + timedelta(
                    seconds=self.settings.unhealthy_after_seconds
                )
                run.state = RunState.RUNNING
                run.started_at = run.started_at or utcnow()
            else:
                attempt.state = AttemptState.FAILED
                attempt.finished_at = utcnow()
                _release(worker, run)
                run.state = RunState.QUEUED
                session.add(
                    OutboxEvent(
                        topic="run.ready",
                        aggregate_id=str(run.id),
                        payload={"run_id": str(run.id), "reason": message.reason},
                    )
                )

    async def _event(self, worker_id: str, message: Any) -> None:
        attempt_id = uuid.UUID(message.attempt_id)
        async with session_factory() as session:
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == attempt_id, Attempt.worker_id == worker_id)
            )
            # Bulk events are persisted by separate workers so a completion can commit
            # first. Keep accepting already-enqueued events for terminal attempts.
            if attempt is None or attempt.state in {AttemptState.OFFERED, AttemptState.LOST}:
                return
            existing = await session.scalar(
                select(RunEventIndex.id).where(
                    RunEventIndex.attempt_id == attempt_id,
                    RunEventIndex.sequence == message.sequence,
                )
            )
            if existing is not None:
                return
            total = sum(
                (
                    await session.scalars(
                        select(RunEventIndex.byte_count).where(
                            RunEventIndex.attempt_id == attempt_id
                        )
                    )
                ).all()
            )
            data = bytes(message.data)
            if total >= self.settings.max_log_bytes:
                return
            data = data[: self.settings.max_log_bytes - total]
            key = f"runs/{attempt.run_id}/{attempt.id}/{message.sequence:020d}.bin"
            await log_store.put(key, data)
            session.add(
                RunEventIndex(
                    run_id=attempt.run_id,
                    attempt_id=attempt.id,
                    sequence=message.sequence,
                    stream=message.stream,
                    object_key=key,
                    byte_count=len(data),
                )
            )
            await session.commit()

    async def _complete(self, worker_id: str, message: Any) -> None:
        state_map = {
            "SUCCEEDED": (AttemptState.SUCCEEDED, RunState.SUCCEEDED),
            "FAILED": (AttemptState.FAILED, RunState.FAILED),
            "CANCELLED": (AttemptState.CANCELLED, RunState.CANCELLED),
            "TIMED_OUT": (AttemptState.TIMED_OUT, RunState.TIMED_OUT),
        }
        if message.terminal_state not in state_map:
            raise ValueError("invalid terminal state")
        async with session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(Attempt).where(Attempt.id == uuid.UUID(message.attempt_id)).with_for_update()
            )
            if (
                attempt is None
                or attempt.worker_id != worker_id
                or not _valid_token(attempt, message.lease_token)
            ):
                raise ValueError("invalid completion")
            if attempt.state not in {AttemptState.OFFERED, AttemptState.RUNNING}:
                return
            run = await session.scalar(
                select(Run).where(Run.id == attempt.run_id).with_for_update()
            )
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if run is None or worker is None:
                raise ValueError("completion references missing state")
            attempt_state, run_state = state_map[message.terminal_state]
            if run.state == RunState.CANCEL_REQUESTED:
                attempt_state, run_state = AttemptState.CANCELLED, RunState.CANCELLED
            attempt.state = attempt_state
            attempt.finished_at = utcnow()
            run.state = run_state
            run.finished_at = utcnow()
            run.result = {"exit_code": message.exit_code}
            if run_state != RunState.SUCCEEDED:
                run.failure_code = message.reason_code or run_state.value
                run.failure_message = message.message
            _release(worker, run)

    async def _cleanup(self, worker_id: str, message: Any) -> None:
        async with session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(Attempt).where(
                    Attempt.id == uuid.UUID(message.attempt_id), Attempt.worker_id == worker_id
                )
            )
            if attempt:
                attempt.cleanup_confirmed = message.successful
                attempt.cleanup_message = message.message

async def serve() -> None:
    service = WorkerControlService()
    service.start()
    server = grpc.aio.server(
        interceptors=[aio_server_interceptor()],  # type: ignore[no-untyped-call]
        options=(("grpc.max_receive_message_length", 4 * 1024 * 1024),),
    )
    worker_pb2_grpc.add_WorkerControlServicer_to_server(  # type: ignore[no-untyped-call]
        service, server
    )
    server.add_insecure_port(get_settings().grpc_bind)
    await server.start()
    logger.info("gRPC gateway listening", extra={"bind": get_settings().grpc_bind})
    try:
        await server.wait_for_termination()
    finally:
        await service.close()


def run() -> None:
    configure_telemetry("agent-fabric-grpc")
    if get_settings().metrics_port:
        start_http_server(get_settings().metrics_port or 0)
    asyncio.run(serve())
