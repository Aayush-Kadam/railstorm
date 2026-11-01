"""Independent bank-book, instruction, journal, mirror and exposure checks."""

from collections import defaultdict

from railstorm.domain import BANKS, digest


def audit_correspondent(state, records, at_ms):
    findings = []
    cb = state["correspondent"]
    accounts, positions, journals = state["accounts"], state["positions"], state["journals"]

    def fail(identity, expected, observed, invariant="CB-ACCOUNTING", severity="ERROR"):
        findings.append(
            {
                "severity": severity,
                "invariant": invariant,
                "identity": identity,
                "expected": expected,
                "observed": observed,
                "recovery_action": "Inspect posted hop/obligation; never erase prior final journal",
            }
        )

    for rel in cb["relationships"].values():
        n, v = rel["nostro_id"], rel["vostro_id"]
        if positions[n]["posted"] + positions[v]["posted"] != 0:
            fail(
                rel["id"],
                "nostro asset = vostro liability",
                [positions[n]["posted"], positions[v]["posted"]],
                "CB-MIRROR",
            )
        if accounts[n]["issuer"] != rel["respondent"] or accounts[v]["issuer"] != rel["correspondent"]:
            fail(rel["id"], "correct institution books", [accounts[n], accounts[v]])
    for bank in BANKS:
        for currency in ("USD", "EUR", "SGD", "INR"):
            if (
                positions[f"GL:{bank}:CASH:{currency}"]["posted"] + positions[f"{bank}:{currency}"]["posted"]
                != 0
            ):
                fail(bank + currency, "GL cash mirrors settlement issuer claim", "mismatch", "CB-CASH-MIRROR")
            total = sum(
                positions[a]["posted"]
                for a, meta in accounts.items()
                if meta["issuer"] == bank and meta["currency"] == currency
            )
            if total:
                fail(bank + currency, "bank book signed sum zero", total)
    by_payment: dict[str, dict] = defaultdict(dict)
    for identity, entry in journals.items():
        if entry["payment_id"]:
            by_payment[entry["payment_id"]][identity] = entry

    def check_journal(identity, currency, issuer, expected):
        entry = journals.get(identity)
        if entry is None:
            fail(identity, "required journal", None)
            return
        changes: dict[str, int] = defaultdict(int)
        for line in entry["lines"]:
            changes[line["account_id"]] += line["delta"]
        actual = {a: n for a, n in changes.items() if n}
        expected = {a: n for a, n in expected.items() if n}
        if entry["currency"] != currency or entry["issuer"] != issuer or actual != expected:
            fail(identity, {"currency": currency, "issuer": issuer, "deltas": expected}, entry)

    exposure: dict[str, dict] = defaultdict(
        lambda: {"amount_minor": 0, "minor_milliseconds": 0, "obligations": 0}
    )
    for record in records:
        payment = record["data"]
        if payment.get("rail") != "CORRESPONDENT":
            continue
        identity = record["id"]
        chain = cb["chains"].get(identity)
        if (
            not chain
            or payment["command"] != record["command"]
            or digest(record["command"]) != record["fingerprint"]
        ):
            fail(identity, "original chain and fingerprinted command", "mismatch")
            continue
        if chain["reference"] != payment["reference"]:
            fail(identity, "consistent end-to-end reference", payment["reference"])
        posted_count = 0
        expected_journal_ids: set[str] = set()
        for i, plan in enumerate(chain["path"]):
            hop = cb["hops"].get(f"{identity}:hop:{i}")
            if (
                not hop
                or any(hop.get(k) != v for k, v in plan.items())
                or hop["reference"] != chain["reference"]
            ):
                fail(identity + str(i), "immutable hop instruction", hop)
                continue
            if hop["posting_status"] != "POSTED":
                if i < payment["next_stage"]:
                    fail(hop["id"], "completed earlier hop", hop["posting_status"])
                continue
            posted_count += 1
            if i and cb["hops"][f"{identity}:hop:{i - 1}"]["posting_status"] != "POSTED":
                fail(hop["id"], "previous hop posted", "out of order")
            previous = cb["obligations"].get(f"{identity}:obligation:{i - 1}") if i else None
            c = plan["currency"]
            incoming, outgoing, fee = plan["input_minor"], plan["output_minor"], plan["fee_minor"]
            if plan["type"] == "HOP":
                rel = cb["relationships"][plan["relationship_id"]]
                config = rel["config"]
                if outgoing != incoming - (
                    config["fixed_fee_minor"] + incoming * config["variable_fee_bps"] // 10000
                ):
                    fail(hop["id"], "independently computed beneficiary fee", fee)
                debit = previous["account_id"] if previous else f"GL:{rel['respondent']}:PAYMENT_EXPENSE:{c}"
                check_journal(
                    hop["id"] + ":respondent",
                    c,
                    rel["respondent"],
                    {debit: -incoming, rel["nostro_id"]: incoming},
                )
                target = (
                    f"BENEFICIARY:{identity}:{c}"
                    if i == len(chain["path"]) - 1
                    else f"TRANSIT:{identity}:{i}:{c}"
                )
                check_journal(
                    hop["id"] + ":correspondent",
                    c,
                    rel["correspondent"],
                    {
                        rel["vostro_id"]: -incoming,
                        target: outgoing,
                        f"GL:{rel['correspondent']}:FEES:{c}": fee,
                    },
                )
                expected_journal_ids.update((hop["id"] + ":respondent", hop["id"] + ":correspondent"))
            else:
                q = state["quotes"].get(plan["quote"]["id"])
                if q is None or previous is None:
                    fail(hop["id"], "FX quote and prior obligation required", q)
                    continue
                if q != plan["quote"] or outgoing != incoming * q["numerator"] // q["denominator"]:
                    fail(hop["id"], "immutable exact FX quote", q)
                bank = plan["sender"]
                target = f"TRANSIT:{identity}:{i}:{plan['target_currency']}"
                check_journal(
                    hop["id"] + ":fx-source",
                    c,
                    bank,
                    {previous["account_id"]: -incoming, f"GL:{bank}:FX:{c}": incoming},
                )
                check_journal(
                    hop["id"] + ":fx-target",
                    plan["target_currency"],
                    bank,
                    {f"GL:{bank}:FX:{plan['target_currency']}": -outgoing, target: outgoing},
                )
                expected_journal_ids.update((hop["id"] + ":fx-source", hop["id"] + ":fx-target"))
            if i < len(chain["path"]) - 1:
                ob = cb["obligations"].get(f"{identity}:obligation:{i}")
                expected_status = (
                    "DISCHARGED"
                    if cb["hops"][f"{identity}:hop:{i + 1}"]["posting_status"] == "POSTED"
                    else "OPEN"
                )
                if payment["economic_status"] == "RETURNED" and i == 0:
                    expected_status = "RETURNED"
                if (
                    not ob
                    or ob["amount"] != outgoing
                    or ob["account_id"] != target
                    or ob["status"] != expected_status
                ):
                    fail(hop["id"], {"amount": outgoing, "status": expected_status}, ob, "CB-OBLIGATION")
                elif positions[target]["posted"] != (outgoing if expected_status == "OPEN" else 0):
                    fail(
                        target,
                        "open debt equals posted payable",
                        positions[target]["posted"],
                        "CB-OBLIGATION",
                    )
        if posted_count != payment["next_stage"]:
            fail(identity, "cursor equals committed hop count", posted_count)
        if payment["economic_status"] == "SETTLED":
            account = f"BENEFICIARY:{identity}:{record['command']['destination_currency']}"
            if (
                posted_count != len(chain["path"])
                or positions.get(account, {}).get("posted") != chain["beneficiary_minor"]
            ):
                fail(identity, "all hops and exact beneficiary credit", "missing", "CB-FINALITY")
            if payment["ack_status"] != "ACKNOWLEDGED":
                fail(
                    identity,
                    "settled; acknowledgment recoverable",
                    payment["ack_status"],
                    "CB-MESSAGING",
                    "WARNING",
                )
        if payment["economic_status"] == "RETURNED":
            first = chain["path"][0]
            rel = cb["relationships"][first["relationship_id"]]
            amount = first["output_minor"]
            target = f"TRANSIT:{identity}:0:{first['currency']}"
            check_journal(
                identity + ":compensation:holder",
                first["currency"],
                rel["correspondent"],
                {target: -amount, rel["vostro_id"]: amount},
            )
            check_journal(
                identity + ":compensation:origin",
                first["currency"],
                rel["respondent"],
                {
                    rel["nostro_id"]: -amount,
                    f"GL:{rel['respondent']}:PAYMENT_EXPENSE:{first['currency']}": amount,
                },
            )
            expected_journal_ids.update(
                (identity + ":compensation:holder", identity + ":compensation:origin")
            )
        if set(by_payment[identity]) != expected_journal_ids:
            fail(
                identity,
                "exact per-hop/compensation journal identities",
                list(by_payment[identity]),
                "CB-IDEMPOTENCY",
            )
    for ob in cb["obligations"].values():
        if ob["status"] == "OPEN":
            metric = exposure[ob["currency"]]
            metric["amount_minor"] += ob["amount"]
            metric["minor_milliseconds"] += ob["amount"] * max(0, at_ms - ob["opened_ms"])
            metric["obligations"] += 1
    return {
        "findings": findings,
        "financial_errors": sum(f["severity"] == "ERROR" for f in findings),
        "exposure_by_currency": dict(exposure),
    }
