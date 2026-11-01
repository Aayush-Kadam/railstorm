"""Replay consumes recorded decisions, never re-quotes or reroutes."""

import hashlib
import json

from railstorm.accounting_audit import economic_findings
from railstorm.domain import digest
from railstorm.store import Store


def empty_state():
    return {
        "accounts": {},
        "positions": {},
        "payments": {},
        "quotes": {},
        "reservations": {},
        "decisions": {},
        "journals": {},
    }


def reduce_event(state: dict, event: dict):
    p, kind = event["payload"], event["event_type"]
    if kind == "WORLD_INITIALIZED" and p.get("config", {}).get("routing"):
        state["routing_records"] = {}
    if kind == "ROUTING_RECORD":
        state.setdefault("routing_records", {})[p["id"]] = p
    if kind == "WORLD_INITIALIZED" and p.get("config", {}).get("instant_messages"):
        state["instant_messages"] = {}
    if kind == "IL_MESSAGE_CHANGED":
        state.setdefault("instant_messages", {})[p["id"]] = p
    if kind.startswith("CB_"):
        cb = state.setdefault(
            "correspondent", {"relationships": {}, "chains": {}, "hops": {}, "obligations": {}}
        )
        if kind == "CB_RELATIONSHIP_OPENED":
            cb["relationships"][p["id"]] = p
        elif kind == "CB_CHAIN_CREATED":
            cb["chains"][p["payment_id"]] = p
        elif kind == "CB_HOP_CHANGED":
            cb["hops"][p["id"]] = p
        elif kind == "CB_OBLIGATION_CHANGED":
            cb["obligations"][p["id"]] = p
    if kind == "ACCOUNT_OPENED":
        state["accounts"][p["id"]] = p
        state["positions"][p["id"]] = {"posted": 0, "reserved": 0, "buffer": 0}
    elif kind == "JOURNAL_POSTED":
        state["journals"][p["id"]] = p
        for line in p["lines"]:
            state["positions"][line["account_id"]]["posted"] += line["delta"]
    elif kind == "RESERVATION_CHANGED":
        old = state["reservations"].get(p["id"])
        if p["status"] == "ACTIVE":
            state["positions"][p["account_id"]]["reserved"] += p["amount"]
        elif old and old["status"] == "ACTIVE":
            state["positions"][p["account_id"]]["reserved"] -= p["amount"]
        state["reservations"][p["id"]] = p
    elif kind == "PAYMENT_CHANGED":
        state["payments"][p["id"]] = p
    elif kind == "QUOTE_LOCKED":
        state["quotes"][p["id"]] = p
    elif kind == "DECISION_CHANGED":
        state["decisions"][p["payment_id"]] = p["decision"]
    elif kind == "BUFFER_CHANGED":
        state["positions"][p["account_id"]]["buffer"] = p["buffer"]


def snapshot(tx):
    state = empty_state()
    for row in tx.rows("SELECT * FROM accounts WHERE run_id=:r ORDER BY id"):
        state["accounts"][row["id"]] = {k: v for k, v in row.items() if k != "run_id"}
    for row in tx.rows("SELECT * FROM positions WHERE run_id=:r ORDER BY account_id"):
        state["positions"][row["account_id"]] = {k: row[k] for k in ("posted", "reserved", "buffer")}
    for row in tx.rows("SELECT * FROM payments WHERE run_id=:r ORDER BY id"):
        state["payments"][row["id"]] = row["data"]
    for row in tx.rows("SELECT * FROM quotes WHERE run_id=:r ORDER BY id"):
        state["quotes"][row["id"]] = row["data"]
    for row in tx.rows("SELECT * FROM reservations WHERE run_id=:r ORDER BY id"):
        state["reservations"][row["id"]] = {k: v for k, v in row.items() if k != "run_id"}
    for row in tx.rows("SELECT * FROM decisions WHERE run_id=:r ORDER BY payment_id"):
        state["decisions"][row["payment_id"]] = row["decision"]
    entries = tx.rows("SELECT * FROM journal_entries WHERE run_id=:r ORDER BY id")
    all_lines = tx.rows(
        "SELECT entry_id,account_id,delta FROM journal_lines WHERE run_id=:r ORDER BY entry_id,line_no"
    )
    grouped: dict[str, list[dict]] = {}
    for line in all_lines:
        grouped.setdefault(line["entry_id"], []).append(
            {"account_id": line["account_id"], "delta": line["delta"]}
        )
    del all_lines
    for row in entries:
        state["journals"][row["id"]] = {k: v for k, v in row.items() if k != "run_id"}
        state["journals"][row["id"]]["lines"] = grouped.get(row["id"], [])
    if tx.run["config"].get("rail") == "CORRESPONDENT":
        cb: dict[str, dict] = {"relationships": {}, "chains": {}, "hops": {}, "obligations": {}}
        for row in tx.rows("SELECT * FROM cb_relationships WHERE run_id=:r ORDER BY id"):
            cb["relationships"][row["id"]] = {k: v for k, v in row.items() if k != "run_id"}
        for row in tx.rows("SELECT * FROM cb_chains WHERE run_id=:r ORDER BY payment_id"):
            cb["chains"][row["payment_id"]] = row["instruction"]
        for row in tx.rows("SELECT data FROM cb_hops WHERE run_id=:r ORDER BY id"):
            cb["hops"][row["data"]["id"]] = row["data"]
        for row in tx.rows("SELECT * FROM cb_obligations WHERE run_id=:r ORDER BY id"):
            cb["obligations"][row["id"]] = {k: v for k, v in row.items() if k != "run_id"}
        state["correspondent"] = cb
    if tx.run["config"].get("instant_messages"):
        state["instant_messages"] = {
            row["id"]: row["data"] for row in tx.rows("SELECT * FROM il_messages WHERE run_id=:r ORDER BY id")
        }
    if tx.run["config"].get("routing"):
        state["routing_records"] = {
            row["id"]: row["data"]
            for row in tx.rows("SELECT * FROM routing_records WHERE run_id=:r ORDER BY id")
        }
    return state


def first_difference(expected, observed, path="state"):
    if type(expected) is not type(observed):
        return path
    if isinstance(expected, dict):
        if expected.keys() != observed.keys():
            return path + ".keys"
        for key in sorted(expected):
            diff = first_difference(expected[key], observed[key], f"{path}.{key}")
            if diff:
                return diff
    elif isinstance(expected, list):
        if len(expected) != len(observed):
            return path + ".length"
        for i, (a, b) in enumerate(zip(expected, observed)):
            diff = first_difference(a, b, f"{path}[{i}]")
            if diff:
                return diff
    elif expected != observed:
        return path
    return None


def replay_check(store: Store, run_id: str):
    with store.transaction(run_id) as tx:
        return _replay_check(tx)


def state_digest(value):
    """Canonical bytes in bounded record chunks using the C JSON encoder.

    Financial snapshots are namespaces of bounded records. Correspondent books
    have one additional namespace level. No cross-transaction data is reused.
    """
    result = hashlib.sha256()

    def encode(item):
        return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

    def feed(item, depth, limit):
        if isinstance(item, dict) and depth < limit and all(isinstance(key, str) for key in item):
            result.update(b"{")
            for index, key in enumerate(sorted(item)):
                if index:
                    result.update(b",")
                result.update(encode(key))
                result.update(b":")
                feed(item[key], depth + 1, 3 if depth == 0 and key == "correspondent" else limit)
            result.update(b"}")
        else:
            result.update(encode(item))

    feed(value, 0, 2)
    return result.hexdigest()


def ordered_events(tx):
    after = 0
    while True:
        page = tx.rows(
            "SELECT * FROM events WHERE run_id=:r AND seq>:after ORDER BY seq LIMIT 512", after=after
        )
        if not page:
            return
        yield from page
        after = page[-1]["seq"]


def _replay_check(tx, actual=None):
    state = empty_state()
    previous = "0" * 64
    divergent = None
    versions: dict[str, int] = {}
    event_count = tx.one("SELECT count(*) n FROM events WHERE run_id=:r")["n"]
    for seq, event in enumerate(ordered_events(tx), 1):
        aggregate = event["aggregate_id"]
        versions[aggregate] = versions.get(aggregate, 0) + 1
        value = {k: v for k, v in event.items() if k not in ("run_id", "event_hash")}
        if (
            event["seq"] != seq
            or event["schema_version"] != 1
            or event["aggregate_version"] != versions[aggregate]
            or event["previous_hash"] != previous
            or digest(value) != event["event_hash"]
        ):
            divergent = seq
            break
        reduce_event(state, event)
        previous = event["event_hash"]
    actual = snapshot(tx) if actual is None else actual
    difference = first_difference(state, actual)
    return {
        "match": divergent is None and difference is None,
        "events": event_count,
        "event_hash": previous,
        "replay_hash": state_digest(state),
        "state_hash": state_digest(actual),
        "earliest_corrupt_event": divergent,
        "projection_difference": difference,
    }


def reconcile(store: Store, run_id: str):
    findings = []

    def finding(invariant, identity, expected, observed, action, severity="ERROR"):
        findings.append(
            {
                "severity": severity,
                "invariant": invariant,
                "identity": identity,
                "expected": expected,
                "observed": observed,
                "recovery_action": action,
            }
        )

    with store.transaction(run_id) as tx:
        state = snapshot(tx)
        findings.extend(
            economic_findings(state, tx.rows("SELECT * FROM payments WHERE run_id=:r ORDER BY id"))
        )
        cb_report = None
        if "correspondent" in state:
            from railstorm.correspondent_audit import audit_correspondent

            cb_report = audit_correspondent(
                state,
                tx.rows("SELECT * FROM payments WHERE run_id=:r ORDER BY id"),
                tx.run["logical_time_ms"],
            )
            findings.extend(cb_report["findings"])
        by_payment: dict[str, list[dict]] = {}
        for entry in state["journals"].values():
            by_payment.setdefault(entry["payment_id"], []).append(entry)
        for identity, entry in state["journals"].items():
            if sum(line["delta"] for line in entry["lines"]) != 0:
                finding("INV-01", identity, "balanced", "unbalanced", "HALT; investigate append-only history")
        for account, pos in state["positions"].items():
            if (
                state["accounts"][account]["kind"] == "CLAIM"
                and pos["posted"] - pos["reserved"] - pos["buffer"] < 0
            ):
                finding("INV-03", account, "nonnegative spendable", pos, "HALT")
            calculated = sum(
                line["delta"]
                for entry in state["journals"].values()
                for line in entry["lines"]
                if line["account_id"] == account
            )
            reserved = sum(
                r["amount"]
                for r in state["reservations"].values()
                if r["account_id"] == account and r["status"] == "ACTIVE"
            )
            if calculated != pos["posted"] or reserved != pos["reserved"]:
                finding(
                    "INV-10",
                    account,
                    {"posted": calculated, "reserved": reserved},
                    pos,
                    "rebuild projection in a separate verified repair",
                )
        for identity, payment in state["payments"].items():
            if payment.get("rail") in {"CORRESPONDENT", "INSTANT_LINK"}:
                continue
            entries = by_payment.get(identity, [])
            if payment["economic_status"] == "SETTLED":
                expected = (
                    1
                    if payment["command"]["source_currency"] == payment["command"]["destination_currency"]
                    else 2
                )
                if len(entries) != expected or state["decisions"].get(identity) != "COMMIT":
                    finding("INV-06", identity, f"{expected} legs and COMMIT", len(entries), "HALT")
                if payment["ack_status"] != "ACKNOWLEDGED":
                    finding(
                        "INV-09",
                        identity,
                        "ACKNOWLEDGED",
                        payment["ack_status"],
                        "resend ACK using original identity",
                        "WARNING",
                    )
            elif entries:
                finding("INV-11", identity, "no postings for unsettled atomic payment", len(entries), "HALT")
        for identity, reservation in state["reservations"].items():
            if reservation["status"] == "ACTIVE":
                p = state["payments"].get(reservation["payment_id"], {})
                if p.get("rail") == "CORRESPONDENT":
                    finding(
                        "INV-12",
                        identity,
                        "no abandoned intra-hop active reservation",
                        p.get("economic_status"),
                        "investigate incomplete hop; never abort prior final legs",
                    )
                    continue
                if p.get("economic_status") != "PREPARED":
                    finding(
                        "INV-12",
                        identity,
                        "live PREPARED payment",
                        p.get("economic_status"),
                        "investigate orphan reservation",
                    )
        # Replay and all contract checks share the same run lock/snapshot.
        replay = _replay_check(tx, state)
    if not replay["match"]:
        finding("INV-10", run_id, "matching replay", replay, "HALT; inspect first divergence")
    result = {
        "financial_errors": sum(f["severity"] == "ERROR" for f in findings),
        "findings": findings,
        "replay": replay,
    }
    if cb_report is not None:
        result["correspondent"] = cb_report
    return result
