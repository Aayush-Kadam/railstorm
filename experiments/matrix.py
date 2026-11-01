"""Eight bounded atomic-rail controls across multiple seeds; not router efficacy."""

import argparse
import csv
import json
from pathlib import Path

from railstorm.scenarios import Scenario
from railstorm.simulation import run_scenario
from railstorm.store import Store

parser = argparse.ArgumentParser()
parser.add_argument(
    "--prefix", default="matrix", help="Use a new prefix for each repeat; history is not reset"
)
args = parser.parse_args()
store = Store()
matrix = [
    ("EXP-01", "healthy_1000", 50, 100),
    ("EXP-02", "healthy_1000", 200, 1),
    ("EXP-03", "sgd_crisis", 100, 10),
    ("EXP-04", "participant_outage", 100, 10),
    ("EXP-05", "fx_shock", 100, 10),
    ("EXP-06", "lost_ack", 1, 10),
    ("EXP-07", "duplicate_storm", 100, 10),
    ("EXP-08", "compound_crisis", 100, 10),
]
output = Path("experiments/reports")
results = []
for experiment, name, count, interval in matrix:
    for seed in [17, 42, 20261029]:
        base = json.loads((Path("scenarios") / f"{name}.json").read_text())
        base.update(seed=seed, payment_count=count, interval_ms=interval)
        scenario = Scenario.model_validate(base)
        result = run_scenario(store, f"{args.prefix}-{experiment}-{seed}", scenario)
        results.append({"experiment": experiment, "seed": seed, "scenario": scenario.model_dump(), **result})
        (output / f"{args.prefix}-progress.json").write_text(json.dumps(results, indent=2))
        print(
            json.dumps(
                {
                    "experiment": experiment,
                    "seed": seed,
                    "settled": result["metrics"]["settled"],
                    "errors": result["verification"]["financial_errors"],
                }
            ),
            flush=True,
        )
(output / f"{args.prefix}.json").write_text(json.dumps(results, indent=2))
with (output / f"{args.prefix}.csv").open("w", newline="") as file:
    writer = csv.writer(file)
    writer.writerow(
        [
            "experiment",
            "seed",
            "submitted",
            "settled",
            "rejected",
            "execution_seconds",
            "financial_errors",
            "replay_match",
        ]
    )
    for r in results:
        writer.writerow(
            [
                r["experiment"],
                r["seed"],
                r["metrics"]["submitted"],
                r["metrics"]["settled"],
                r["metrics"]["statuses"].get("REJECTED", 0),
                r["execution_wall_seconds"],
                r["verification"]["financial_errors"],
                r["verification"]["replay"]["match"],
            ]
        )
