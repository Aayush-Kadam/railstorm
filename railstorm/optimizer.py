"""Exact online minimax local funding-share policy; no price/probability forecasts."""

from fractions import Fraction


def choose(candidates, policy):
    if policy.version == "LIQUIDITY_AWARE_V2":
        from railstorm.optimizer_v2 import choose_v2

        return choose_v2(candidates, policy)
    feasible = [c for c in candidates if c["status"] == "FEASIBLE" and c["rail"] in policy.preference]
    if policy.version == "fixed-order-v1":
        selected = next((r for r in policy.preference if any(c["rail"] == r for c in feasible)), None)
        return selected, []
    if policy.version != "LIQUIDITY_AWARE_V1":
        raise ValueError("UNSUPPORTED_POLICY")
    scored = []
    currency_order = list(
        dict.fromkeys(
            (feasible[0]["currencies"] if feasible else [])
            + sorted({currency for candidate in feasible for currency in candidate["fees_by_currency"]})
        )
    )
    for c in feasible:
        shares = [Fraction(row["required_minor"], row["available_minor"]) for row in c["liquidity"]]
        pressure = max(shares, default=Fraction(0))
        currencies = currency_order
        fees = tuple(c["fees_by_currency"].get(currency, 0) for currency in currencies)
        rank = (
            pressure,
            fees,
            c["latency_ms"] is None,
            c["latency_ms"] or 0,
            policy.preference.index(c["rail"]),
            c["route_id"],
        )
        score = {
            "rail": c["rail"],
            "max_local_funding_share": {"numerator": pressure.numerator, "denominator": pressure.denominator},
            "fee_currency_order": currencies,
            "fee_vector_minor": list(fees),
            "modeled_latency_ms": c["latency_ms"],
            "preference_index": policy.preference.index(c["rail"]),
            "uncertainty": "Only current account liquidity; no downstream reservation or future-liquidity guarantee",
        }
        scored.append((rank, score))
    scored.sort(key=lambda pair: pair[0])
    return (scored[0][1]["rail"] if scored else None), [score for _, score in scored]
