"""Bounded, isolated Compose demo and real-service regressions (Python stdlib only)."""

import argparse
import json
import platform
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare", action="store_true", help="pull/build only (outside demo budget)"
    )
    parser.add_argument("--regressions", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results/tiny-demo")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = "agent-fabric-demo-" + uuid.uuid4().hex[:12]
    base = ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.demo.yml")]
    transcript = []

    def command(argv, timeout, check=True):
        transcript.append("$ " + " ".join(argv))
        print(transcript[-1], flush=True)
        result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        transcript.extend([result.stdout, result.stderr, f"exit_code={result.returncode}"])
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", flush=True)
        if check and result.returncode:
            raise RuntimeError(f"command failed: {argv} (exit {result.returncode})")
        return result.stdout

    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "project": project,
        "mode": "prepare" if args.prepare else "demo",
    }
    status = 1
    started = time.monotonic()
    try:
        metadata["commit"] = command(["git", "rev-parse", "HEAD"], 10).strip()
        metadata["dirty"] = bool(command(["git", "status", "--porcelain"], 10).strip())
        metadata["docker"] = command(["docker", "version", "--format", "{{json .}}"], 15)
        metadata["compose"] = command(["docker", "compose", "version"], 15).strip()
        if args.prepare:
            command(base + ["--profile", "tools", "pull", "--ignore-buildable"], 300)
            command(base + ["--profile", "tools", "build"], 600)
        else:
            # A UUID project cannot reuse old worker rows or interfere with other demos.
            command(base + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "45"], 55)
            raw = command(base + ["run", "--rm", "--no-deps", "demo"], 70)
            (args.output / "result.json").write_text(raw, encoding="utf-8")
            if args.regressions:
                command(base + ["run", "--rm", "--no-deps", "regressions"], 90)
        status = 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        transcript.append(f"ERROR: {exc}")
        print(transcript[-1], flush=True)
    finally:
        if not args.prepare:
            try:
                command(base + ["logs", "--no-color", "--tail", "100"], 10, check=False)
                command(base + ["down", "--volumes", "--remove-orphans", "--timeout", "5"], 20)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                transcript.append(f"CLEANUP ERROR: {exc}; retry {' '.join(base)} down --volumes")
                status = 1
        metadata.update(exit_code=status, elapsed_seconds=round(time.monotonic() - started, 3))
        (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (args.output / "transcript.txt").write_text("\n".join(transcript) + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
