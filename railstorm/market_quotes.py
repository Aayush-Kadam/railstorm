"""Append-only synthetic market versions, serialized with financial commands."""

import json

from sqlalchemy import text

from railstorm.domain import Currency, DomainError


def current(tx, source, target):
    # Buffered events have larger sequence numbers than persisted events. Merge
    # this exact query's read-your-writes view without flushing unrelated events.
    # No value survives the transaction, and no projection/audit result is cached.
    pair = f"{source}/{target}"
    for event in reversed(tx.event_rows):
        if event["event_type"] == "MARKET_QUOTE":
            payload = json.loads(event["payload"])
            if payload.get("pair") == pair:
                return payload
    sql = (
        "SELECT payload FROM events WHERE run_id=:r AND event_type='MARKET_QUOTE' "
        "AND payload->>'pair'=:pair ORDER BY seq DESC LIMIT 1"
    )
    parameters = {"r": tx.run_id, "pair": pair}
    commands = getattr(tx, "commands", None)
    result = (
        commands.execute(sql, parameters) if commands is not None else tx.conn.execute(text(sql), parameters)
    )
    row = result.mappings().first()
    return row["payload"] if row else None


def replace(store, run, *, pair, version, numerator, denominator, at_ms, expires_ms, fee_minor=25):
    """Introduce a market version for native operator execution, not a routing promise.

    Routing validation must independently match the immutable terms before preparing.
    """
    parts = pair.split("/")
    if len(parts) != 2 or any(p not in Currency._value2member_map_ for p in parts) or parts[0] == parts[1]:
        raise DomainError("INVALID_MARKET_PAIR")
    if any(type(v) is not int for v in (version, numerator, denominator, at_ms, expires_ms, fee_minor)):
        raise DomainError("INVALID_MARKET_QUOTE")
    if min(version, numerator, denominator) <= 0 or not 0 <= at_ms < expires_ms or fee_minor < 0:
        raise DomainError("INVALID_MARKET_QUOTE")
    with store.transaction(run) as tx:
        tx.time(at_ms)
        if tx.run["config"].get("rail", "ATOMIC_PVP") not in ("ATOMIC_PVP", "INSTANT_LINK"):
            raise DomainError("MARKET_UPDATE_UNSUPPORTED_RAIL")
        old = current(tx, *parts)
        baseline_versions = [
            q["version"]
            for q in tx.run["config"].get("quote_schedule", [])
            if q["pair"] == pair and q["valid_from_ms"] <= at_ms
        ]
        highest = max([old["version"] if old else 0, *baseline_versions])
        if version <= highest:
            raise DomainError("STALE_MARKET_VERSION")
        if tx.run["config"].get("rail") == "INSTANT_LINK" and (pair != "USD/SGD" or fee_minor != 25):
            raise DomainError("UNSUPPORTED_INSTANT_MARKET_TERMS")
        quote = {
            "id": f"market:{pair}:{version}",
            "pair": pair,
            "version": version,
            "numerator": numerator,
            "denominator": denominator,
            "valid_from_ms": at_ms,
            "expires_ms": expires_ms,
            "fee_minor": fee_minor,
            "provenance": "DETERMINISTIC_SYNTHETIC_MARKET",
        }
        tx.event("MARKET_QUOTE", pair, quote)
        return quote
