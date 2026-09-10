"""Render a comparison from retained historical data; never synthesizes measurements."""

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache/matplotlib"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

source = ROOT / "benchmarks/reports/2026-09-02-parallel-scheduler/evidence.json"
output = ROOT / "benchmarks/reports/2026-09-10-readiness"
output.mkdir(parents=True, exist_ok=True)
data = json.loads(source.read_text())
rows = data["replica_sweep"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12, "svg.hashsalt": "agent-fabric"})
fig, ax = plt.subplots(figsize=(10, 5.6), facecolor="#f5f7fa")
ax.set_facecolor("#f5f7fa")
values = [r["effective_placements_per_second"] for r in rows]
bars = ax.bar(
    range(len(rows)),
    values,
    width=0.58,
    color=["#187d8d" if r["retry_attempts"] == 0 else "#b86220" for r in rows],
)
for bar, row in zip(bars, rows, strict=True):
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 2,
        f"{row['effective_placements_per_second']:.2f}",
        ha="center",
        weight="bold",
    )
ax.set_xticks(
    range(len(rows)),
    [
        f"{r['scheduler_processes']} scheduler"
        + ("s" if r["scheduler_processes"] > 1 else "")
        + f"\n{r['retry_attempts']} retries"
        for r in rows
    ],
)
ax.set_ylabel("Effective placements / second")
ax.set_ylim(0, 110)
ax.spines[["top", "right"]].set_visible(False)
ax.set_axisbelow(True)
ax.grid(axis="y", color="#dbe1e8")
fig.suptitle(
    "Two schedulers peaked; eight failed the zero-retry gate",
    x=0.075,
    ha="left",
    weight="bold",
    fontsize=17,
)
ax.set_title(
    "Historical simulated execution · 50,000 gRPC streams · 10,000 total jobs / phase",
    loc="left",
    fontsize=11,
    pad=18,
)
fig.text(
    0.075,
    0.035,
    "2 Sep 2026 · 24 vCPU / 62.5 GiB Docker allocation · summary-only retained evidence\nNo scale rerun. Full 1/2/4/8 acceptance remains false. Source: parallel-scheduler/evidence.json",
    fontsize=10,
    color="#445064",
)
fig.tight_layout(rect=(0.03, 0.12, 0.98, 0.92))
fig.savefig(output / "scheduler-comparison.png", dpi=160)
fig.savefig(output / "scheduler-comparison.svg", metadata={"Date": None})
plt.close(fig)
(output / "chart-provenance.json").write_text(
    json.dumps(
        {
            "source": str(source.relative_to(ROOT)),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "generator": "scripts/evidence_chart.py",
            "matplotlib": matplotlib.__version__,
            "command": "uv run --locked --extra evidence python scripts/evidence_chart.py",
            "interpretation": "Historical summary comparison; no fresh benchmark or error bars available",
        },
        indent=2,
    )
    + "\n"
)
print("Generated scheduler comparison from committed historical JSON")
