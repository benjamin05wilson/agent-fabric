import uuid
from datetime import UTC, datetime

from agent_fabric.models import Project, Run, Worker
from agent_fabric.scheduler import Capacity, Placement, Scheduler


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


def capacity(name: str, cpu: int = 4000, memory: int = 4096, pids: int = 512) -> Capacity:
    return Capacity(
        id=name,
        cpu_millis=cpu,
        memory_mb=memory,
        pids=pids,
        free_cpu=cpu,
        free_memory=memory,
        free_pids=pids,
    )


def run(project: Project, cpu: int = 1000, memory: int = 512, priority: int = 5) -> Run:
    return Run(
        id=uuid.uuid4(),
        project_id=project.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash="x",
        spec={
            "repository": {"url": "https://example.com/r", "ref": "HEAD"},
            "argv": ["true"],
            "environment": {},
            "profile": "python",
            "network": "disabled",
            "resources": {
                "cpu_millis": cpu,
                "memory_mb": memory,
                "pids": 16,
                "disk_mb": 128,
                "timeout_seconds": 30,
            },
        },
        priority=priority,
        attempt_count=0,
        created_at=datetime.now(UTC),
    )


def project(max_running: int = 20, weight: int = 1) -> Project:
    return Project(
        id=uuid.uuid4(), slug="p", api_key_hash=b"0" * 32, weight=weight, max_running=max_running
    )


def test_capacity_fit() -> None:
    spec = {"resources": {"cpu_millis": 2000, "memory_mb": 2048, "pids": 100}}
    assert Scheduler._fits(worker(), spec)


def test_capacity_rejects_overcommit() -> None:
    spec = {"resources": {"cpu_millis": 3500, "memory_mb": 2048, "pids": 100}}
    assert not Scheduler._fits(worker(), spec)


def test_batch_plan_packs_without_overcommit() -> None:
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 100,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    tenant = project(max_running=100)
    runs = [run(tenant) for _ in range(10)]
    capacities = [capacity("a"), capacity("b")]
    placements = scheduler._plan(runs, capacities, {tenant.id: tenant}, {})
    # Two workers with 4000 cpu_millis each fit exactly eight 1000-millicpu runs.
    assert len(placements) == 8
    assert {p.worker_id for p in placements} == {"a", "b"}
    assert all(c.free_cpu >= 0 and c.free_memory >= 0 for c in capacities)
    assert len({p.run_id for p in placements}) == 8


def test_batch_plan_respects_project_running_limit() -> None:
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 100,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    tenant = project(max_running=3)
    runs = [run(tenant, cpu=100, memory=128) for _ in range(10)]
    placements = scheduler._plan(runs, [capacity("a")], {tenant.id: tenant}, {tenant.id: 1})
    assert len(placements) == 2


def test_batch_plan_prefers_higher_priority() -> None:
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 1,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    tenant = project()
    low, high = run(tenant, priority=1), run(tenant, priority=9)
    placements = scheduler._plan([low, high], [capacity("a")], {tenant.id: tenant}, {})
    assert placements[0].run_id == high.id


def test_batch_plan_honours_outstanding_offer_limit() -> None:
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 100,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    tenant = project(max_running=100)
    runs = [run(tenant, cpu=100, memory=128) for _ in range(10)]
    placements = scheduler._plan(runs, [capacity("a")], {tenant.id: tenant}, {}, limit=3)
    assert len(placements) == 3


def test_gpu_capacity_and_capability_are_both_required() -> None:
    resources = {"cpu_millis": 1000, "memory_mb": 1024, "pids": 16, "gpu": 1, "vram_mb": 8192}
    cpu_only = capacity("cpu")
    gpu = Capacity(
        id="gpu",
        cpu_millis=8000,
        memory_mb=16384,
        pids=512,
        free_cpu=8000,
        free_memory=16384,
        free_pids=512,
        gpu_count=1,
        vram_mb=16384,
        free_gpu=1,
        free_vram=16384,
        capabilities=frozenset({"cuda"}),
    )
    assert not cpu_only.fits(resources, ["cuda"])
    assert gpu.fits(resources, ["cuda"])
    gpu.reserve(resources)
    assert not gpu.fits(resources, ["cuda"])


def test_cpu_jobs_preserve_gpu_workers() -> None:
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 1,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    tenant = project()
    gpu = capacity("gpu")
    gpu.gpu_count = gpu.free_gpu = 1
    gpu.vram_mb = gpu.free_vram = 16384
    gpu.capabilities = frozenset({"cuda"})
    placement = scheduler._plan([run(tenant)], [gpu, capacity("cpu")], {tenant.id: tenant}, {})
    assert placement[0].worker_id == "cpu"


def test_commit_trim_enforces_global_offer_and_tenant_limits() -> None:
    first = project(max_running=2)
    second = project(max_running=10)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.deficits = {}
    scheduler.settings = type(
        "S",
        (),
        {
            "scheduler_batch_size": 10,
            "acknowledgement_deadline_seconds": 10,
            "profile_images": {"python": "python:3.12-slim"},
        },
    )()
    runs = [run(first, cpu=100, memory=128) for _ in range(3)] + [
        run(second, cpu=100, memory=128) for _ in range(3)
    ]
    planned = scheduler._plan(
        runs,
        [capacity("worker", cpu=10000)],
        {first.id: first, second.id: second},
        {},
    )

    accepted = Scheduler._trim_for_commit(
        planned,
        {first.id: first, second.id: second},
        {first.id: 1},
        available_offers=3,
    )

    assert len(accepted) == 3
    assert sum(item.project_id == first.id for item in accepted) == 1


def test_commit_trim_accepts_no_more_than_available_offers() -> None:
    tenant = project(max_running=100)
    placement = Placement(
        run_id=uuid.uuid4(),
        project_id=tenant.id,
        worker_id="worker",
        attempt_number=1,
        resources={},
        payload={},
        token_hash="hash",
        expires=datetime.now(UTC),
    )

    assert Scheduler._trim_for_commit([placement], {tenant.id: tenant}, {}, 0) == []
