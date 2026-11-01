"""Independent economic-contract checks, not a replay or production posting call."""

from collections import defaultdict

from railstorm.domain import digest


def economic_findings(state: dict, records: list[dict]) -> list[dict]:
    findings = []

    def fail(identity, invariant, expected, observed):
        findings.append(
            {
                "severity": "ERROR",
                "invariant": invariant,
                "identity": identity,
                "expected": expected,
                "observed": observed,
                "recovery_action": "HALT; investigate immutable quote, original command and journals",
            }
        )

    entries: dict[str, list[dict]] = defaultdict(list)
    holds: dict[str, list[dict]] = defaultdict(list)
    for entry in state["journals"].values():
        if entry["payment_id"] is not None:
            entries[entry["payment_id"]].append(entry)
    for reservation in state["reservations"].values():
        holds[reservation["payment_id"]].append(reservation)
    for record in records:
        if record["data"].get("rail") in {"CORRESPONDENT", "INSTANT_LINK"}:
            continue
        identity, command = record["id"], record["command"]
        payment = state["payments"][identity]
        if payment.get("command") != command or digest(command) != record["fingerprint"]:
            fail(identity, "INV-05", "original fingerprinted command", "command projection mismatch")
        status = payment.get("economic_status")
        if status == "PREPARED" and state["decisions"].get(identity) != "PREPARED":
            fail(
                identity,
                "INV-06",
                "PREPARED decision for uncommitted preparation",
                state["decisions"].get(identity),
            )
        if status not in {"CREATED", "PREPARED", "SETTLED", "REJECTED", "ABORTED", "EXPIRED"}:
            fail(identity, "INV-11", "known economic state", status)
        quote = state["quotes"].get(f"q:{identity}")
        if quote is None:
            if status in {"PREPARED", "SETTLED", "ABORTED", "EXPIRED"}:
                fail(identity, "INV-08", "immutable accepted quote", None)
            continue
        if payment.get("quote") != quote:
            fail(identity, "INV-08", "immutable quote equals payment view", "quote projection mismatch")
        try:
            source, target = command["source_currency"], command["destination_currency"]
            principal, fee, output = command["principal_minor"], quote["fee_minor"], quote["output_minor"]
            valid = (
                type(principal) is int
                and type(fee) is int
                and type(output) is int
                and quote["numerator"] > 0
                and quote["denominator"] > 0
                and fee >= 0
                and quote["payment_id"] == identity
                and quote["source"] == source
                and quote["target"] == target
                and quote["fee_currency"] == source
                and quote["principal_minor"] == principal
                and output == principal * quote["numerator"] // quote["denominator"]
                and fee <= command["max_fee_minor"]
                and output >= command["minimum_receive_minor"]
            )
            if not valid:
                fail(identity, "INV-08", "exact accepted rational quote within command limits", quote)
            requirements = {f"{command['sender_bank_id']}:{source}": principal + fee}
            if source != target:
                provider = f"STERLING_UK:{target}"
                requirements[provider] = requirements.get(provider, 0) + output
            expiry = min(quote["expires_ms"], payment["deadline_ms"])
            expected_hold_status = {
                "PREPARED": "ACTIVE",
                "SETTLED": "CONSUMED",
                "ABORTED": "RELEASED",
                "EXPIRED": "RELEASED",
            }.get(status)
            if expected_hold_status:
                expected_holds = {
                    f"{identity}:r{i}": (account, amount, expiry, expected_hold_status)
                    for i, (account, amount) in enumerate(sorted(requirements.items()))
                }
                observed_holds = {
                    r["id"]: (r["account_id"], r["amount"], r["expires_ms"], r["status"])
                    for r in holds[identity]
                }
                if observed_holds != expected_holds:
                    fail(identity, "INV-07", expected_holds, observed_holds)
            if status in {"ABORTED", "EXPIRED"} and state["decisions"].get(identity) != "ABORT":
                fail(identity, "INV-12", "ABORT decision", state["decisions"].get(identity))
            if status != "SETTLED":
                continue
            expected_source: dict[str, int] = defaultdict(int)
            expected_source[f"{command['sender_bank_id']}:{source}"] -= principal + fee
            expected_source[
                f"{command['receiver_bank_id']}:{target}" if source == target else f"STERLING_UK:{source}"
            ] += principal
            expected_source[f"OPERATOR:{source}"] += fee
            expected_entries = {
                f"{identity}:source": (source, {a: n for a, n in expected_source.items() if n})
            }
            if source != target:
                target_lines: dict[str, int] = defaultdict(int)
                target_lines[f"STERLING_UK:{target}"] -= output
                target_lines[f"{command['receiver_bank_id']}:{target}"] += output
                expected_entries[f"{identity}:target"] = (
                    target,
                    {a: n for a, n in target_lines.items() if n},
                )
            observed_entries = {}
            for entry in entries[identity]:
                changes: dict[str, int] = defaultdict(int)
                for line in entry["lines"]:
                    changes[line["account_id"]] += line["delta"]
                observed_entries[entry["id"]] = (entry["currency"], {a: n for a, n in changes.items() if n})
                if entry["issuer"] != "SHARED":
                    fail(identity, "INV-01", "SHARED issuer", entry["issuer"])
            if observed_entries != expected_entries:
                fail(identity, "INV-08", expected_entries, observed_entries)
            if payment.get("settlement_id") != f"settlement:{identity}" or not (
                payment["submitted_ms"] <= payment["settled_ms"] < expiry
            ):
                fail(
                    identity,
                    "INV-11",
                    "stable settlement identity and execution within exclusive expiry",
                    payment.get("settlement_id"),
                )
        except (KeyError, TypeError, ZeroDivisionError) as exc:
            fail(identity, "INV-08", "well-formed economic contract", type(exc).__name__)
    from railstorm.instant_audit import instant_findings

    findings.extend(instant_findings(state, records))
    return findings
