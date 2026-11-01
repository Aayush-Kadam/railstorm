"""Operational parallelization only: run frozen independent scenario blocks."""

import hashlib
import json
import sys
import time
from pathlib import Path

from experiments.p1c_o1_campaign import memory, plans, run_group
from railstorm.domain import digest
from railstorm.store import Store

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
frozen = json.loads((docs / "P1C_O1_frozen_v2.json").read_text())
for name, expected in frozen["source_sha256"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
plan = json.loads((docs / "P1C_O1_plan_v2.json").read_text())
assert frozen["plan_hash"] == digest(plan)
entries = [p for p in plans() if p["stage"] == "primary"]
store = Store()
for value in sys.argv[1:]:
    index = int(value)
    assert 0 <= index <= 9
    target = docs / f"P1C_O1_v2_block_{index:02d}.json"
    if target.exists():
        saved = json.loads(target.read_text())
        assert saved["status"] == "COMPLETE" and saved["result"]["plan"] == entries[index]
        continue
    started = time.perf_counter()
    before = memory()
    print("Frozen block", index, entries[index]["family"], flush=True)
    result = run_group(store, entries[index])
    result["resource"] = {
        "before": before,
        "after": memory(),
        "scope": "Windows client only; concurrent scenario processes; database excluded",
    }
    target.write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "plan_hash": frozen["plan_hash"],
                "result": result,
                "block_wall_seconds": time.perf_counter() - started,
            },
            indent=2,
        )
    )
    print("Completed frozen block", index, flush=True)
