"""Record small deterministic routing baseline evidence without policy efficacy claims."""

import json
from pathlib import Path

from railstorm.routing_experiments import SCENARIOS, experiment
from railstorm.store import Store

store = Store()
results = {key: experiment(store, key) for key in SCENARIOS}
Path("docs/P1C_D0_experiments.json").write_text(json.dumps(results, indent=2, default=str))
print(
    {
        key: {
            "selected": r["record"]["evaluation"]["selected"],
            "routing_replay": r["routing_replay"]["match"],
            "financial_errors": sum(a["financial_errors"] for a in r["audits"].values()),
        }
        for key, r in results.items()
    }
)
