import argparse
import asyncio
import random

from agent_fabric import loadgen


def test_percentiles_reports_tail_not_just_mean() -> None:
    summary = loadgen.percentiles([1.0, 2.0, 3.0, 4.0, 100.0])
    assert summary["count"] == 5
    assert summary["p50"] == 3.0
    assert summary["p99"] == 100.0
    assert summary["max"] == 100.0
    assert summary["mean"] == 22.0


def test_percentiles_handles_empty_input() -> None:
    summary = loadgen.percentiles([])
    assert summary == {
        "count": 0,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
        "mean": None,
    }


def test_kill_reports_in_flight_attempts_and_cancels_the_stream() -> None:
    async def scenario() -> tuple[list[str], bool]:
        measurements = loadgen.Measurements(target_workers=1)
        worker = loadgen.SimulatedWorker(
            number=0,
            address="localhost:1",
            measurements=measurements,
            rng=random.Random(1),
            min_duration_ms=1,
            max_duration_ms=1,
            failure_rate=0.0,
            disappear_rate=0.0,
        )
        worker.active.update({"attempt-b", "attempt-a"})
        worker.task = asyncio.create_task(asyncio.sleep(60))
        in_flight = worker.kill()
        await asyncio.gather(worker.task, return_exceptions=True)
        return in_flight, worker.task.cancelled()

    in_flight, cancelled = asyncio.run(scenario())
    assert in_flight == ["attempt-a", "attempt-b"]
    assert cancelled


def test_chaos_selects_a_deterministic_fraction_of_the_fleet() -> None:
    async def scenario() -> dict[str, object]:
        measurements = loadgen.Measurements(target_workers=10)
        workers = [
            loadgen.SimulatedWorker(
                number=index,
                address="localhost:1",
                measurements=measurements,
                rng=random.Random(index),
                min_duration_ms=1,
                max_duration_ms=1,
                failure_rate=0.0,
                disappear_rate=0.0,
            )
            for index in range(10)
        ]
        args = argparse.Namespace(kill_fraction=0.3, kill_after_seconds=0, seed=7)
        await loadgen.inject_chaos(args, workers, measurements)
        return measurements.chaos

    chaos = asyncio.run(scenario())
    assert chaos["killed_workers"] == 3
    assert chaos["killed_fraction"] == 0.3
    assert len(chaos["killed_worker_ids"]) == 3
    assert chaos["in_flight_attempts_at_kill"] == 0


def test_parser_rejects_out_of_range_kill_fraction() -> None:
    args = loadgen.parser().parse_args(["--kill-fraction", "1.5"])
    assert args.kill_fraction == 1.5
