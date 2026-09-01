from agent_fabric.models import Worker
from agent_fabric.scheduler import Scheduler


def worker() -> Worker:
    return Worker(
        id="worker-1",
        protocol_version="v1",
        worker_version="test",
        cpu_millis=4000,
        memory_mb=4096,
        pids=512,
        reserved_cpu_millis=1000,
        reserved_memory_mb=1024,
        reserved_pids=10,
        capabilities=[],
        sandbox_backends=["gvisor"],
    )


def test_capacity_fit() -> None:
    spec = {"resources": {"cpu_millis": 2000, "memory_mb": 2048, "pids": 100}}
    assert Scheduler._fits(worker(), spec)


def test_capacity_rejects_overcommit() -> None:
    spec = {"resources": {"cpu_millis": 3500, "memory_mb": 2048, "pids": 100}}
    assert not Scheduler._fits(worker(), spec)
