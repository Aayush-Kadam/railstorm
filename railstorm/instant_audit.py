"""Independent instant-link economic checks over persisted projections."""

from railstorm.domain import digest


def instant_findings(state, records):
    errors = []
    for record in records:
        p = record["id"]
        s = state["payments"][p]
        if s.get("rail") != "INSTANT_LINK":
            continue

        def check(ok, reason):
            if not ok:
                errors.append(
                    {
                        "severity": "ERROR",
                        "invariant": "IL-ECONOMIC",
                        "identity": p,
                        "expected": reason,
                        "observed": "mismatch",
                        "recovery_action": "HALT",
                    }
                )

        check(
            s["command"] == record["command"] and digest(record["command"]) == record["fingerprint"],
            "immutable command",
        )
        entries = {k: v for k, v in state["journals"].items() if v["payment_id"] == p}
        holds = {k: v for k, v in state["reservations"].items() if v["payment_id"] == p}
        expected = {}
        check(len(holds) in (0, 2), "exact hold count")
        q = state["quotes"].get(f"q:{p}")
        check(q is not None or s["economic_status"] == "REJECTED", "quote required for accepted payment")
        if "instant_messages" in state:
            messages = [m for m in state["instant_messages"].values() if m["payment_id"] == p]
            stages = {m["type"] for m in messages}
            check(
                stages == ({"SOURCE", "DESTINATION"} if s["source_final"] else {"SOURCE"} if q else set()),
                "required durable messages",
            )
            for m in messages:
                check(
                    m["id"] == p + ":" + m["type"].lower() + "-message"
                    and m["destination"] == ("US_FAST" if m["type"] == "SOURCE" else "SG_FAST")
                    and m["business_reference"] == p,
                    "typed message identity",
                )
                check(len({a["id"] for a in m["attempts"]}) == len(m["attempts"]), "unique delivery attempts")
        if q:
            n = record["command"]["principal_minor"]
            out = n * 133 // 100
            check(
                q == s["quote"]
                and q["principal_minor"] == n
                and q["output_minor"] == out
                and q["fee_minor"] == 25,
                "exact quote",
            )
            expired = s["economic_status"] == "EXPIRED"
            for suffix, account, amount, final in [
                ("source-hold", "ATLAS_US:USD", n + 25, s["source_final"]),
                ("destination-hold", "STERLING_UK:SGD", out, s["destination_final"]),
            ]:
                r = holds.get(f"{p}:{suffix}", {})
                check(
                    (r.get("account_id"), r.get("amount"), r.get("status"))
                    == (account, amount, "CONSUMED" if final else "RELEASED" if expired else "ACTIVE"),
                    "exact funded reservation lifecycle",
                )
            if s["source_final"]:
                expected[f"{p}:us-final"] = (
                    "USD",
                    {"ATLAS_US:USD": -n - 25, "STERLING_UK:USD": n, "OPERATOR:USD": 25},
                )
                check(state["decisions"].get(p) == "COMMIT", "durable commit authority")
            if s["destination_final"]:
                expected[f"{p}:sg-final"] = ("SGD", {"STERLING_UK:SGD": -out, "MERIDIAN_SG:SGD": out})
            if s["source_final"] and not s["destination_final"]:
                o = s["obligation"] or {}
                check(
                    (o.get("amount_minor"), o.get("currency"), o.get("owner")) == (out, "SGD", "STERLING_UK"),
                    "funded outstanding SGD obligation",
                )
            else:
                check(s["obligation"] is None, "no outstanding obligation")
        observed = {
            k: (v["currency"], {line["account_id"]: line["delta"] for line in v["lines"]})
            for k, v in entries.items()
        }
        for key, entry in entries.items():
            check(
                entry["issuer"] == ("US_FAST" if key.endswith(":us-final") else "SG_FAST"),
                "domestic settlement issuer",
            )
        check(observed == expected, "exact separately final journals")
        check(
            (s["economic_status"] == "SETTLED") == (s["source_final"] and s["destination_final"]),
            "completion requires both final legs",
        )
    return errors
