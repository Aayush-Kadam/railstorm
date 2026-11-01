"""One exact, untuned Pareto-cost / exposure policy. No financial mutations."""

from contextvars import ContextVar
from fractions import Fraction

# Experiment-only ablations; API has no switch. Mode is retained in durable scores.
diagnostic_mode: ContextVar[str] = ContextVar("v2_diagnostic_mode", default="full")


def choose_v2(candidates, policy):
    mode = diagnostic_mode.get()
    if mode not in ("full", "cost_only", "exposure_only"):
        raise ValueError("INVALID_DIAGNOSTIC_MODE")
    feasible = [c for c in candidates if c["status"] == "FEASIBLE" and c["rail"] in policy.preference]
    currencies = list(
        dict.fromkeys(
            (feasible[0]["currencies"] if feasible else [])
            + sorted({x for c in feasible for x in c["fees_by_currency"]})
        )
    )

    def dominates(a, b):
        fees_a = tuple(a["fees_by_currency"].get(x, 0) for x in currencies)
        fees_b = tuple(b["fees_by_currency"].get(x, 0) for x in currencies)
        proceeds_a, proceeds_b = a["beneficiary_minor"], b["beneficiary_minor"]
        return (
            all(x <= y for x, y in zip(fees_a, fees_b, strict=True))
            and proceeds_a >= proceeds_b
            and (fees_a != fees_b or proceeds_a > proceeds_b)
        )

    survivors = (
        feasible
        if mode == "exposure_only"
        else [c for c in feasible if not any(dominates(other, c) for other in feasible)]
    )
    scored = []
    for c in survivors:
        observation = c["exposure_observation"]
        exposure = tuple(
            observation["open_by_currency"].get(x, 0) + observation["potential_peak_by_currency"].get(x, 0)
            for x in currencies
        )
        holds = tuple(observation["nominal_hold_minor_ms_by_currency"].get(x, 0) for x in currencies)
        scarcity = sum(
            (Fraction(r["required_minor"], r["available_minor"]) for r in c["liquidity"]), start=Fraction(0)
        )
        tail = (
            c["latency_ms"] is None,
            c["latency_ms"] or 0,
            policy.preference.index(c["rail"]),
            c["route_id"],
        )
        rank = tail if mode == "cost_only" else (exposure, holds, scarcity, *tail)
        scored.append(
            (
                rank,
                {
                    "rail": c["rail"],
                    "diagnostic_mode": mode,
                    "currency_order": currencies,
                    "cost_frontier": [s["rail"] for s in survivors],
                    "excluded_cost_dominated": [s["rail"] for s in feasible if s not in survivors],
                    "fees_by_currency": c["fees_by_currency"],
                    "beneficiary_minor": c["beneficiary_minor"],
                    "exposure_vector_minor": list(exposure),
                    "nominal_hold_vector_minor_ms": list(holds),
                    "sum_local_scarcity": {
                        "numerator": scarcity.numerator,
                        "denominator": scarcity.denominator,
                    },
                    "modeled_latency_ms": c["latency_ms"],
                    "uncertainty": "Potential exposure and nominal holds are estimates, not committed reservations or future guarantees",
                },
            )
        )
    scored.sort(key=lambda item: item[0])
    return (scored[0][1]["rail"] if scored else None), [s for _, s in scored]


def observe_exposure(tx, candidate):
    """Observe native obligations once; never add nostro mirrors as separate funds."""
    rail = candidate["rail"]
    opened: dict[str, int] = {}
    potential: dict[str, int] = {}
    holds: dict[str, int] = {}
    if rail == "CORRESPONDENT":
        for row in tx.rows("SELECT currency,amount FROM cb_obligations WHERE run_id=:r AND status='OPEN'"):
            opened[row["currency"]] = opened.get(row["currency"], 0) + row["amount"]
        for stage in candidate.get("path", []):
            currency = stage.get("target_currency", stage["currency"])
            potential[currency] = max(potential.get(currency, 0), stage["output_minor"])
            if stage["type"] == "HOP":
                # Native hop reserve/consume is one transaction, so no modeled holding interval.
                holds.setdefault(stage["currency"], 0)
    elif rail == "INSTANT_LINK":
        for row in tx.rows("SELECT data FROM payments WHERE run_id=:r"):
            obligation = row["data"].get("obligation")
            if obligation:
                x = obligation["currency"]
                opened[x] = opened.get(x, 0) + obligation["amount_minor"]
        potential[candidate["currencies"][1]] = candidate["beneficiary_minor"] or 0
    if rail != "CORRESPONDENT":
        for row in candidate["liquidity"]:
            x = row["currency"]
            holds[x] = holds.get(x, 0) + row["required_minor"] * (candidate["latency_ms"] or 0)
    return {
        "open_by_currency": opened,
        "potential_peak_by_currency": potential,
        "nominal_hold_minor_ms_by_currency": holds,
        "semantics": "Current obligations plus per-currency potential peak; no failure probabilities",
    }
