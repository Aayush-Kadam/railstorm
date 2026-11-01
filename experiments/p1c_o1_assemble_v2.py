"""Assemble the untouched corrected-scheduler cohort exactly once."""

import hashlib
import json
from pathlib import Path

from experiments.p1c_o1_campaign import plans

root = Path(__file__).resolve().parents[1] / "docs"
frozen = json.loads((root / "P1C_O1_frozen_v2.json").read_text())
groups, provenance = [], []
entries = [p for p in plans() if p["stage"] == "primary"]
for index, entry in enumerate(entries):
    source = root / f"P1C_O1_v2_block_{index:02d}.json"
    saved = json.loads(source.read_text())
    assert saved["status"] == "COMPLETE" and saved["plan_hash"] == frozen["plan_hash"]
    group = saved["result"]
    assert group["plan"] == entry and group["fairness_checked"]
    groups.append(group)
    provenance.append(
        {
            "index": index,
            "family": entry["family"],
            "source": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    )
assert len({r["physical_runs"]["control"] for g in groups for r in g["policies"]}) == 30
target = root / "P1C_O1_primary_v2_results.json"
assert not target.exists()
target.write_text(
    json.dumps(
        {
            "plan_hash": frozen["plan_hash"],
            "status": "COMPLETE",
            "runs": groups,
            "assembly_provenance": provenance,
            "note": "Only the new1021/2039/4093 cohort contributes to efficacy; interrupted prior131/257/509 records are excluded.",
        },
        indent=2,
    )
)
print("Assembled ten untouched blocks,30 policy runs,3000 assigned intents")
