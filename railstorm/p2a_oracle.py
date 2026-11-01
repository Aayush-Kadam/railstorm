"""Read-only, separately expressed SQL checks of durable financial state."""

import hashlib
import json
from collections import defaultdict

from sqlalchemy import text


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def independent_audit(store, run_id):
    """Inspect durable tables without invoking posting, reconciliation or replay code."""
    findings = []
    with store.engine.connect() as conn:

        def rows(sql):
            return [dict(row) for row in conn.execute(text(sql), {"r": run_id}).mappings()]

        accounts = {a["id"]: a for a in rows("SELECT * FROM accounts WHERE run_id=:r")}
        positions = {p["account_id"]: p for p in rows("SELECT * FROM positions WHERE run_id=:r")}
        entries = rows("SELECT * FROM journal_entries WHERE run_id=:r")
        lines = rows("SELECT * FROM journal_lines WHERE run_id=:r ORDER BY entry_id,line_no")
        reservations = rows("SELECT * FROM reservations WHERE run_id=:r")
        payments = {p["id"]: p["data"] for p in rows("SELECT * FROM payments WHERE run_id=:r")}
        events = rows("SELECT * FROM events WHERE run_id=:r ORDER BY seq")
        obligations = rows("SELECT * FROM cb_obligations WHERE run_id=:r")
        messages = rows("SELECT * FROM il_messages WHERE run_id=:r")
        by_entry = defaultdict(list)
        by_account: dict[str, int] = defaultdict(int)
        for line in lines:
            by_entry[line["entry_id"]].append(line)
            by_account[line["account_id"]] += line["delta"]
        for entry in entries:
            group = by_entry[entry["id"]]
            if len(group) < 2 or sum(x["delta"] for x in group) != 0:
                findings.append({"check": "DOUBLE_ENTRY", "identity": entry["id"]})
            if any(accounts.get(x["account_id"], {}).get("currency") != entry["currency"] for x in group):
                findings.append({"check": "CURRENCY_BOUNDARY", "identity": entry["id"]})
            if any(accounts.get(x["account_id"], {}).get("issuer") != entry["issuer"] for x in group):
                findings.append({"check": "ISSUER_BOUNDARY", "identity": entry["id"]})
        if len(by_entry) != len(entries):
            findings.append({"check": "ORPHAN_JOURNAL_LINES", "identity": run_id})
        active: dict[str, int] = defaultdict(int)
        for hold in reservations:
            if hold["status"] == "ACTIVE":
                active[hold["account_id"]] += hold["amount"]
                if hold["payment_id"] not in payments:
                    findings.append({"check": "ORPHAN_HOLD", "identity": hold["id"]})
                elif payments[hold["payment_id"]]["economic_status"] not in (
                    "PREPARED",
                    "PENDING_EXPOSURE",
                    "QUEUED",
                ):
                    findings.append({"check": "HOLD_LIFECYCLE", "identity": hold["id"]})
        for account, position in positions.items():
            if by_account[account] != position["posted"] or active[account] != position["reserved"]:
                findings.append({"check": "POSITION_PROJECTION", "identity": account})
            if accounts[account]["kind"] == "CLAIM" and (
                position["posted"] - position["reserved"] - position["buffer"] < 0
            ):
                findings.append({"check": "NEGATIVE_AVAILABILITY", "identity": account})
        previous = "0" * 64
        versions: dict[str, int] = defaultdict(int)
        for number, event in enumerate(events, 1):
            versions[event["aggregate_id"]] += 1
            value = {k: v for k, v in event.items() if k not in ("run_id", "event_hash")}
            if (
                event["seq"] != number
                or event["previous_hash"] != previous
                or event["aggregate_version"] != versions[event["aggregate_id"]]
                or _hash(value) != event["event_hash"]
            ):
                findings.append({"check": "EVENT_CHAIN", "identity": number})
                break
            previous = event["event_hash"]
        for payment_id, payment in payments.items():
            if payment.get("rail") == "INSTANT_LINK":
                if payment.get("economic_status") == "SETTLED" and not (
                    payment.get("source_final") and payment.get("destination_final")
                ):
                    findings.append({"check": "INSTANT_FINALITY", "identity": payment_id})
                if (
                    payment.get("source_final")
                    and not payment.get("destination_final")
                    and not payment.get("obligation")
                ):
                    findings.append({"check": "MISSING_INSTANT_OBLIGATION", "identity": payment_id})
            if payment.get("rail") == "ATOMIC_PVP" and payment.get("economic_status") != "SETTLED":
                if any(e["payment_id"] == payment_id for e in entries):
                    findings.append({"check": "ATOMIC_PARTIAL_POSTING", "identity": payment_id})
        for obligation in obligations:
            if obligation["status"] == "OPEN" and obligation["payment_id"] not in payments:
                findings.append({"check": "ORPHAN_CORRESPONDENT_OBLIGATION", "identity": obligation["id"]})
    return {
        "run_id": run_id,
        "pass": not findings,
        "findings": findings,
        "accounts": len(accounts),
        "payments": len(payments),
        "journal_groups": len(entries),
        "journal_lines": len(lines),
        "reservations": len(reservations),
        "correspondent_obligations": len(obligations),
        "instant_messages": len(messages),
        "events": len(events),
        "event_hash": previous,
        "financial_state_hash": _hash(
            {
                "positions": {k: {x: y for x, y in v.items() if x != "run_id"} for k, v in positions.items()},
                "payments": payments,
                "reservations": [{x: y for x, y in v.items() if x != "run_id"} for v in reservations],
                "obligations": [{x: y for x, y in v.items() if x != "run_id"} for v in obligations],
                "messages": [{x: y for x, y in v.items() if x != "run_id"} for v in messages],
            }
        ),
    }
