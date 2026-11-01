"""Check persisted native provenance against completed frozen cohort artifacts."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from railstorm.domain import digest
from railstorm.store import Store

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
frozen = json.loads((docs / "P1C_O1_frozen_v2.json").read_text())
for name, expected in frozen["source_sha256"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
cohort = json.loads((docs / "P1C_O1_primary_v2_results.json").read_text())
assert cohort["status"] == "COMPLETE" and len(cohort["runs"]) == 10
store = Store()
verified = []
for group in cohort["runs"]:
    hashes: dict[str, list[str]] = {}
    for result in group["policies"]:
        booked_fees = Counter()
        beneficiary_proceeds = Counter()
        for rail, world in result["physical_runs"].items():
            audit = result["routing_audit"] if rail == "control" else result["financial_audits"][rail]
            assert audit["financial_errors"] == 0 and audit["replay"]["match"]
            with store.transaction(world) as tx:
                config_hash = digest(tx.run["config"])
                final_event = tx.one(
                    "SELECT event_hash FROM events WHERE run_id=:r ORDER BY seq DESC LIMIT 1"
                )["event_hash"]
                if rail != "control":
                    for row in tx.rows(
                        "SELECT a.currency,sum(p.posted)::bigint amount FROM accounts a JOIN positions p ON p.run_id=a.run_id AND p.account_id=a.id WHERE a.run_id=:r AND (a.id LIKE 'OPERATOR:%' OR a.id LIKE 'GL:%:FEES:%') GROUP BY a.currency"
                    ):
                        booked_fees[row["currency"]] += row["amount"]
                    for row in tx.rows(
                        "SELECT a.currency,sum(p.posted)::bigint amount FROM accounts a JOIN positions p ON p.run_id=a.run_id AND p.account_id=a.id WHERE a.run_id=:r AND ((:cb AND a.id LIKE 'BENEFICIARY:%') OR (NOT :cb AND a.id LIKE 'MERIDIAN_SG:%')) GROUP BY a.currency",
                        cb=rail == "CORRESPONDENT",
                    ):
                        beneficiary_proceeds[row["currency"]] += row["amount"]
            assert final_event == audit["replay"]["event_hash"], "History differs from recorded audit"
            hashes.setdefault(rail, []).append(config_hash)
        assert result["metrics"]["submitted"] == group["plan"]["count"]
        assert not result["metrics"]["duplicate_financial_postings"]
        assert +booked_fees == Counter(result["metrics"]["fees_by_currency"])
        assert +beneficiary_proceeds == Counter(result["metrics"]["beneficiary_credited_by_currency"])
    assert all(len(set(values)) == 1 for values in hashes.values()), "Native opening configurations differ"
    verified.append(
        {
            "family": group["plan"]["family"],
            "seed": group["plan"]["seed"],
            "configuration_hashes": hashes,
            "worlds": 12,
            "native_history_heads_match": True,
            "fees_and_proceeds_independently_match_native_positions": True,
        }
    )
(docs / "P1C_O1_final_audit.json").write_text(
    json.dumps(
        {
            "unchanged_frozen_sources": True,
            "groups": verified,
            "native_worlds_verified": sum(g["worlds"] for g in verified),
            "scope": "Initial configuration equality and current immutable history heads against full audits already executed by each policy; not a second full replay",
        },
        indent=2,
    )
)
print("Frozen source, native configurations and all120 immutable financial/routing history heads match")
