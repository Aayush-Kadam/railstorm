"""Frozen small cohort, checkpointed sequentially; never tune against held-out outcomes."""

import hashlib
import json
from pathlib import Path

from railstorm.domain import digest
from railstorm.optimizer_experiments import compare, specification
from railstorm.store import Store

root = Path(__file__).resolve().parents[1]
planned = [
    {
        "scenario": f"OPT-{i:02d}",
        "seed": 11 if i <= 3 else 71,
        "count": 5,
        "split": "development" if i <= 3 else "held-out",
        "primary": i != 6,
    }
    for i in range(1, 11)
]
planned += [
    {"scenario": scenario, "seed": 97, "count": 5, "split": "sensitivity", "primary": True}
    for scenario in ("OPT-01", "OPT-02")
]
source_files = [
    "railstorm/optimizer.py",
    "railstorm/optimizer_experiments.py",
    "railstorm/routing.py",
    "railstorm/ledger.py",
    "railstorm/rails/correspondent.py",
    "railstorm/rails/instant_link.py",
    "railstorm/store.py",
    "railstorm/audit.py",
]
protocol = {
    "version": "optimizer-o0-cohort-v1",
    "selection_rule": "minimax current account funding share; then common currency fee vector, nominal latency, fixed preference",
    "primary_outcome": "financially completed assigned intents by horizon; same denominator per pair",
    "tuning": "none; fixed rule frozen after one-payment development/debugging checks",
    "planned": [
        {**row, "specification": specification(row["scenario"], row["seed"], row["count"])} for row in planned
    ],
    "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_files},
    "unsupported": "OPT-06 synchronous FX rate repricing unsupported; availability proxy executed, excluded from primary aggregate",
    "interpretation": "Exploratory deterministic fixtures; no stochastic generalization or confidence interval claim",
}
protocol_path = root / "docs/P1C_O0_PREDECLARED.json"
if protocol_path.exists():
    assert json.loads(protocol_path.read_text()) == protocol, "Protocol changed; do not overwrite outcomes"
else:
    with protocol_path.open("x", encoding="utf-8") as f:
        json.dump(protocol, f, indent=2)
output = root / "docs/P1C_O0_results.json"
result = (
    json.loads(output.read_text())
    if output.exists()
    else {"protocol_hash": digest(protocol), "runs": [], "status": "IN_PROGRESS"}
)
assert result["protocol_hash"] == digest(protocol)
store = Store()
for index, entry in enumerate(planned):
    if index < len(result["runs"]):
        continue
    print(
        f"Starting {index + 1}/{len(planned)} {entry['scenario']} seed={entry['seed']} split={entry['split']}",
        flush=True,
    )
    try:
        pair = compare(store, entry["scenario"], entry["seed"], entry["count"])
        result["runs"].append({"plan": entry, "comparison": pair})
    except Exception as exc:
        result["status"] = "FAILED_STOP"
        result["failed_plan"] = entry
        result["error_type"] = type(exc).__name__
        output.write_text(json.dumps(result, indent=2))
        raise
    output.write_text(json.dumps(result, indent=2))
    print(
        [
            (
                p["policy"]["version"],
                p["metrics"]["completed"],
                p["metrics"]["pending"],
                p["metrics"]["fees_by_currency"],
            )
            for p in pair["policies"]
        ],
        flush=True,
    )
result["status"] = "COMPLETE"
output.write_text(json.dumps(result, indent=2))
print("Cohort complete:12 pairs,24 isolated runs,120 assigned payment intents", flush=True)
