"""Persist C1 golden/outage economic and replay evidence using real PostgreSQL."""

import json
import uuid
from pathlib import Path

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

store = Store()
rail = InstantLink(store)
results = {}
for scenario in ["IL-01", "IL-05"]:
    run = "c1-evidence-" + uuid.uuid4().hex
    rail.initialize(run)
    cmd = PaymentCommand(idempotency_key="golden", principal_minor=10000, requested_rail="INSTANT_LINK")
    rail.prepare(run, cmd)
    rail.advance(run, "golden", 10)
    rail.advance(run, "golden", 30, destination_available=scenario != "IL-05")
    before = rail.inspect(run, "golden")
    audit = reconcile(store, run)
    replay = replay_check(store, run)
    result = {"run_id": run, "before_restoration": before, "audit": audit, "replay": replay}
    if scenario == "IL-05":
        rail.advance(run, "golden", 70000)
        result["after_restoration"] = rail.inspect(run, "golden")
        result["recovery_audit"] = reconcile(store, run)
        result["recovery_replay"] = replay_check(store, run)
    results[scenario] = result
Path("docs/P1B_C1_observed.json").write_text(json.dumps(results, indent=2, default=str))
print(
    json.dumps(
        {
            k: {
                "run": v["run_id"],
                "phase": v["before_restoration"]["payment"]["phase"],
                "errors": v["audit"]["financial_errors"],
                "replay": v["replay"]["match"],
            }
            for k, v in results.items()
        }
    )
)
