import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import grpc
from opentelemetry.instrumentation.grpc import aio_server_interceptor
from prometheus_client import start_http_server
from redis.asyncio import Redis
from sqlalchemy import select

from .config import get_settings
from .db import session_factory
from .generated import worker_pb2, worker_pb2_grpc
from .log_store import log_store
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


class WorkerControlService(worker_pb2_grpc.WorkerControlServicer):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)

    async def Connect(
        self,
        request_iterator: AsyncIterator[worker_pb2.WorkerMessage],
        context: grpc.aio.ServicerContext[worker_pb2.WorkerMessage, worker_pb2.ControlMessage],
    ) -> AsyncIterator[worker_pb2.ControlMessage]:
        outgoing: asyncio.Queue[worker_pb2.ControlMessage | None] = asyncio.Queue(maxsize=1000)
        worker_id: str | None = None
        dispatcher: asyncio.Task[None] | None = None

        async def receive() -> None:
            nonlocal worker_id, dispatcher
            try:
                async for message in request_iterator:
                    if not worker_id:
                        if message.WhichOneof("payload") != "register" or not message.worker_id:
                            await context.abort(
                                grpc.StatusCode.FAILED_PRECONDITION, "register first"
                            )
                            return
                        worker_id = message.worker_id
                        await self._register(worker_id, message.register)
                        dispatcher = asyncio.create_task(
                            self._dispatch(worker_id, outgoing), name=f"dispatch-{worker_id}"
                        )
                        continue
                    if message.worker_id != worker_id:
                        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker id changed")
                        return
                    kind = message.WhichOneof("payload")
                    if kind == "heartbeat":
                        await self._heartbeat(worker_id, message.heartbeat)
                    elif kind == "acknowledgement":
                        await self._acknowledge(worker_id, message.acknowledgement)
                    elif kind == "event":
                        await self._event(worker_id, message.event)
                    elif kind == "completion":
                        await self._complete(worker_id, message.completion)
                    elif kind == "cleanup":
                        await self._cleanup(worker_id, message.cleanup)
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
            receiver.cancel()
            if dispatcher:
                dispatcher.cancel()
            tasks = [receiver, *(task for task in [dispatcher] if task is not None)]
            await asyncio.gather(*tasks, return_exceptions=True)

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
        expiry = now + timedelta(seconds=self.settings.unhealthy_after_seconds)
        active_ids = []
        for raw_id in message.active_attempt_ids:
            try:
                active_ids.append(uuid.UUID(raw_id))
            except ValueError:
                continue
        async with session_factory() as session, session.begin():
            worker = await session.get(Worker, worker_id, with_for_update=True)
            if worker is None:
                raise ValueError("worker is not registered")
            worker.last_seen_at = now
            if active_ids:
                attempts = (
                    await session.scalars(
                        select(Attempt).where(
                            Attempt.id.in_(active_ids),
                            Attempt.worker_id == worker_id,
                            Attempt.state == AttemptState.RUNNING,
                        )
                    )
                ).all()
                for attempt in attempts:
                    attempt.lease_expires_at = expiry
        await self.redis.hset(
            "af:worker:liveness",
            worker_id,
            json.dumps({"seen": now.timestamp(), "active": list(message.active_attempt_ids)}),
        )

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
            if attempt is None or attempt.state != AttemptState.RUNNING:
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

    async def _dispatch(
        self, worker_id: str, outgoing: asyncio.Queue[worker_pb2.ControlMessage | None]
    ) -> None:
        stream_ids = {f"af:lease.offer.{worker_id}": "$", "af:run.cancel": "$"}
        while True:
            messages = await self.redis.xread(stream_ids, block=1000, count=20)
            for stream, entries in messages:
                for entry_id, fields in entries:
                    stream_ids[stream] = entry_id
                    payload = json.loads(fields["payload"])
                    if stream.endswith("run.cancel"):
                        run_id = payload["run_id"]
                        async with session_factory() as session:
                            attempt = await session.scalar(
                                select(Attempt).where(
                                    Attempt.run_id == uuid.UUID(run_id),
                                    Attempt.worker_id == worker_id,
                                    Attempt.state == AttemptState.RUNNING,
                                )
                            )
                        if attempt:
                            await outgoing.put(
                                worker_pb2.ControlMessage(
                                    cancel=worker_pb2.CancelRun(
                                        run_id=run_id, attempt_id=str(attempt.id)
                                    )
                                )
                            )
                        continue
                    await outgoing.put(
                        worker_pb2.ControlMessage(lease=worker_pb2.LeaseOffer(**payload))
                    )


async def serve() -> None:
    server = grpc.aio.server(
        interceptors=[aio_server_interceptor()],  # type: ignore[no-untyped-call]
        options=(("grpc.max_receive_message_length", 4 * 1024 * 1024),),
    )
    worker_pb2_grpc.add_WorkerControlServicer_to_server(  # type: ignore[no-untyped-call]
        WorkerControlService(), server
    )
    server.add_insecure_port(get_settings().grpc_bind)
    await server.start()
    logger.info("gRPC gateway listening", extra={"bind": get_settings().grpc_bind})
    await server.wait_for_termination()


def run() -> None:
    configure_telemetry("agent-fabric-grpc")
    if get_settings().metrics_port:
        start_http_server(get_settings().metrics_port or 0)
    asyncio.run(serve())
