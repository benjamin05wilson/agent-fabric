"""Submit one benign checkout-reading fixture to a prepared native Linux worker."""

import argparse
import json
import time
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    deadline = time.monotonic() + 120

    def request(path, body=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fixture deadline exceeded")
        req = urllib.request.Request(
            args.api + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer af_dev_key",
                "Content-Type": "application/json",
                "Idempotency-Key": "fixture-" + uuid.uuid4().hex,
            },
        )
        with urllib.request.urlopen(req, timeout=min(5, remaining)) as response:
            return json.load(response)

    body = {
        "repository": {"url": "https://github.com/octocat/Hello-World", "ref": "master"},
        "argv": [
            "python",
            "-c",
            "from pathlib import Path; s=Path('README').read_text().strip(); assert s == 'Hello World!'; print('fixture: '+s)",
        ],
        "profile": "python",
        "network": "disabled",
        "resources": {
            "cpu_millis": 1000,
            "memory_mb": 128,
            "pids": 16,
            "disk_mb": 128,
            "timeout_seconds": 20,
        },
    }
    run_id = request("/runs", body)["id"]
    print("run_id=" + run_id, flush=True)
    while time.monotonic() < deadline:
        run = request("/runs/" + run_id)
        if run["state"] in {"FAILED", "LOST", "CANCELLED", "TIMED_OUT"}:
            raise RuntimeError(json.dumps(run))
        if run["state"] == "SUCCEEDED":
            logs = request("/runs/" + run_id + "/logs")
            if "fixture: Hello World!" in "".join(row["data"] for row in logs["records"]):
                print(json.dumps({"run": run, "logs": logs}, indent=2))
                return
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise TimeoutError("fixture did not succeed with expected checkout contents within 120 s")


if __name__ == "__main__":
    main()
