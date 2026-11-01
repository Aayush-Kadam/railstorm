"""Actual wall-time measurements; no scientific routing claim from one rail."""

import argparse
import csv
import json
import platform
import sys
from pathlib import Path

from railstorm import __version__
from railstorm.scenarios import Scenario
from railstorm.simulation import run_scenario
from railstorm.store import Store

parser = argparse.ArgumentParser()
parser.add_argument("--scenario", default="scenarios/stress_10000.json")
parser.add_argument("--seeds", nargs="+", type=int, default=[20261029])
parser.add_argument("--prefix", required=True)
args = parser.parse_args()
store = Store()
results = []
out = Path("experiments/reports")
out.mkdir(parents=True, exist_ok=True)
for seed in args.seeds:
    scenario = Scenario.model_validate_json(Path(args.scenario).read_text()).model_copy(update={"seed": seed})
    result = run_scenario(store, f"{args.prefix}-{seed}", scenario)
    result["runtime_context"] = {
        "python": sys.version,
        "platform": platform.platform(),
        "engine": __version__,
    }
    results.append(result)
    target = out / f"{args.prefix}-{seed}.json"
    target.write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "report": str(target),
                "wall_seconds": result["execution_wall_seconds"],
                "settled": result["metrics"]["settled"],
                "errors": result["verification"]["financial_errors"],
            }
        ),
        flush=True,
    )
with (out / f"{args.prefix}.csv").open("w", newline="") as file:
    writer = csv.DictWriter(
        file,
        fieldnames=[
            "run",
            "submitted",
            "settled",
            "p95_ms",
            "fees_usd_minor",
            "execution_seconds",
            "financial_errors",
            "replay_match",
        ],
    )
    writer.writeheader()
    for r in results:
        writer.writerow(
            {
                "run": r["run_id"],
                **{k: r["metrics"][k] for k in ("submitted", "settled", "p95_ms", "fees_usd_minor")},
                "execution_seconds": r["execution_wall_seconds"],
                "financial_errors": r["verification"]["financial_errors"],
                "replay_match": r["verification"]["replay"]["match"],
            }
        )
