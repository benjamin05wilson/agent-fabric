import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

from agent_fabric.generated import worker_pb2
from agent_fabric.grpc_server import WorkerControlService


class FakePipeline:
    def __init__(self) -> None:
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    def xack(self, *args: Any) -> None:
        self.operations.append(("xack", args))

    def xdel(self, *args: Any) -> None:
        self.operations.append(("xdel", args))

    async def execute(self) -> None:
        return None


class FakeRedis:
    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner
        self.added: list[tuple[str, dict[str, str]]] = []
        self.pipeline_value = FakePipeline()

    async def hget(self, key: str, worker_id: str) -> str | None:
        assert key == "af:worker:owners"
        assert worker_id == "worker-1"
        return self.owner

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> None:
        assert maxlen == 100_000
        assert approximate
        self.added.append((stream, fields))

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert not transaction
        return self.pipeline_value


def make_service(redis: FakeRedis) -> WorkerControlService:
    service = WorkerControlService.__new__(WorkerControlService)
    service.gateway_id = "grpc-0"
    service.connections = {}
    service.redis = redis  # type: ignore[assignment]
    return service


async def test_outbound_lease_is_delivered_to_local_connection() -> None:
    redis = FakeRedis()
    service = make_service(redis)
    queue: asyncio.Queue[worker_pb2.ControlMessage | None] = asyncio.Queue()
    service.connections["worker-1"] = queue
    payload = {
        "attempt_id": "attempt-1",
        "run_id": "run-1",
        "lease_token": "secret",
        "expires_unix_millis": 123,
    }

    delivered = await service._deliver_outbound(
        "af:gateway:grpc-0:outbound",
        "gateway",
        "1-0",
        {"worker_id": "worker-1", "kind": "lease", "payload": json.dumps(payload)},
    )

    assert delivered
    message = queue.get_nowait()
    assert message is not None
    assert message.lease.attempt_id == "attempt-1"
    assert redis.added == []
    assert [operation for operation, _ in redis.pipeline_value.operations] == ["xack", "xdel"]


async def test_pending_message_is_forwarded_when_worker_changes_shards() -> None:
    redis = FakeRedis(owner="grpc-7")
    service = make_service(redis)
    fields = {"worker_id": "worker-1", "kind": "cancel", "payload": "{}"}

    delivered = await service._deliver_outbound(
        "af:gateway:grpc-0:outbound", "gateway", "2-0", fields
    )

    assert delivered
    assert redis.added == [("af:gateway:grpc-7:outbound", fields)]
    assert [operation for operation, _ in redis.pipeline_value.operations] == ["xack", "xdel"]


async def test_pending_message_waits_for_worker_to_reconnect() -> None:
    redis = FakeRedis(owner="grpc-0")
    service = make_service(redis)

    delivered = await service._deliver_outbound(
        "af:gateway:grpc-0:outbound",
        "gateway",
        "3-0",
        {"worker_id": "worker-1", "kind": "cancel", "payload": "{}"},
    )

    assert not delivered
    assert redis.added == []
    assert redis.pipeline_value.operations == []


async def test_bulk_event_writer_processes_queued_event() -> None:
    redis = FakeRedis()
    service = make_service(redis)
    service.event_queue = asyncio.Queue()
    service._event = AsyncMock()  # type: ignore[method-assign]
    event = worker_pb2.RunEvent(attempt_id="attempt-1", sequence=7, data=b"log")
    writer = asyncio.create_task(service._event_writer())

    try:
        await service.event_queue.put(("worker-1", event))
        await asyncio.wait_for(service.event_queue.join(), timeout=1)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    service._event.assert_awaited_once_with("worker-1", event)
