"""Preserve interrupted v1 evaluation histories without treating them as efficacy."""

import gzip
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import snapshot
from railstorm.optimizer_experiments import _history
from railstorm.store import Store

root = Path(__file__).resolve().parents[1] / "docs"
store = Store()
with store.engine.connect() as connection:
    controls = (
        connection.execute(
            text(
                "SELECT r.id FROM runs r WHERE r.config->>'routing'='true' AND EXISTS (SELECT 1 FROM routing_records rr WHERE rr.run_id=r.id AND rr.id='p0000' AND rr.data->'evaluation'->'intent'->>'execute_until_ms'='11500') ORDER BY r.id"
            )
        )
        .scalars()
        .all()
    )
target = root / "P1C_O1_interrupted_native.jsonl.gz"
assert not target.exists()
inventory = []
with gzip.open(target, "wt", encoding="utf-8") as output:
    for control in controls:
        for world in (control, *(control + "-" + r for r in ("atomic_pvp", "correspondent", "instant_link"))):
            with store.transaction(world) as tx:
                state, metadata = snapshot(tx), tx.run
            events = _history(store, world)
            output.write(
                json.dumps({"world": world, "metadata": metadata, "state": state, "events": events}) + "\n"
            )
            inventory.append(
                {
                    "world": world,
                    "event_count": len(events),
                    "last_head": events[-1]["event_hash"],
                    "payments": len(state["payments"]),
                    "logical_time_ms": metadata["logical_time_ms"],
                }
            )
(root / "P1C_O1_interrupted_inventory.json").write_text(
    json.dumps(
        {
            "classification": "Interrupted validation cohort; excluded from final efficacy totals. Includes completed and partial worlds, not independent extra samples.",
            "reason": "Queued unfunded first correspondent hop retried every1ms rather than100ms; pathological full-audit busy loop, no observed financial invariant failure.",
            "policy_change": "NONE; V2 decision rule retained byte-identical",
            "native_export": target.name,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "worlds": inventory,
        },
        indent=2,
    )
)
print("Preserved", len(inventory), "interrupted native histories and snapshots")
