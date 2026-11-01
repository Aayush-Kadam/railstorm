"""Independent integer reference and fresh SQL reads for every accepted timing world."""

import json
import random
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import replay_check
from railstorm.p2a_oracle import independent_audit
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"


def main():
    context = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    contract = json.loads((OUT / "contract.json").read_text(encoding="utf-8"))
    source = Store()
    store = Store(source.engine.url.set(database=context["database"]).render_as_string(hide_password=False))
    results = []
    for count, prefix in ((100, "accepted"), (500, "stable1"), (1000, "stable1")):
        rng = random.Random(contract["scenario"]["seed"])
        amounts = [
            contract["scenario"]["principal_minor"]
            + rng.randint(-contract["scenario"]["jitter_minor"], contract["scenario"]["jitter_minor"])
            for _ in range(count)
        ]
        outputs = [amount * 133 // 100 for amount in amounts]
        expected = {
            f"{bank}:{currency}": 0
            for bank in (
                "ATLAS_US",
                "STERLING_UK",
                "RHINE_EU",
                "MERIDIAN_SG",
                "BHARAT_IN",
                "OPENING",
                "OPERATOR",
            )
            for currency in ("USD", "EUR", "SGD", "INR")
        }
        for account, amount in contract["funding"].items():
            expected[account] += amount
            expected["OPENING:" + account.split(":")[1]] -= amount
        expected["ATLAS_US:USD"] -= sum(amounts) + 20 * count
        expected["STERLING_UK:USD"] += sum(amounts)
        expected["STERLING_UK:SGD"] -= sum(outputs)
        expected["MERIDIAN_SG:SGD"] += sum(outputs)
        expected["OPERATOR:USD"] += 20 * count
        for i, implementation in enumerate(("baseline", "modified", "modified", "baseline")):
            measured = json.loads(
                (OUT / f"{prefix}-{count}-{i}-{implementation}.json").read_text(encoding="utf-8")
            )
            run = measured["run_id"]
            with store.engine.connect() as conn:

                def rows(sql):
                    return [dict(r) for r in conn.execute(text(sql), {"r": run}).mappings()]

                positions = rows(
                    "SELECT account_id,posted,reserved,buffer FROM positions WHERE run_id=:r ORDER BY account_id"
                )
                payments = rows("SELECT id,command,data FROM payments WHERE run_id=:r ORDER BY id")
                decisions = rows(
                    "SELECT payment_id,decision FROM decisions WHERE run_id=:r ORDER BY payment_id"
                )
                lines = rows(
                    "SELECT entry_id,line_no,account_id,currency,issuer,delta FROM journal_lines WHERE run_id=:r AND entry_id LIKE 'p%' ORDER BY entry_id,line_no"
                )
                quotes = {
                    r["payment_id"]: r["data"]
                    for r in rows("SELECT payment_id,data FROM quotes WHERE run_id=:r")
                }
                reservations = rows("SELECT payment_id,status FROM reservations WHERE run_id=:r")
                orphan = rows(
                    "SELECT count(*) n FROM events e LEFT JOIN outbox o ON e.run_id=o.run_id AND e.seq=o.seq WHERE e.run_id=:r AND o.seq IS NULL"
                )[0]["n"]
            assert {r["account_id"]: r["posted"] for r in positions} == expected
            assert all(r["reserved"] == r["buffer"] == 0 for r in positions)
            assert len(payments) == len(decisions) == len(quotes) == count
            assert all(r["decision"] == "COMMIT" for r in decisions)
            assert len(reservations) == count * 2 and all(r["status"] == "CONSUMED" for r in reservations)
            assert orphan == 0
            expected_lines = []
            for index, (amount, output, payment) in enumerate(zip(amounts, outputs, payments, strict=True)):
                identity = f"p{index:06d}"
                data = payment["data"]
                assert payment["id"] == identity and payment["command"]["principal_minor"] == amount
                assert data["economic_status"] == "SETTLED" and data["ack_status"] == "ACKNOWLEDGED"
                assert data["settled_ms"] == index * 10 + 50
                quote = quotes[identity]
                assert (
                    data["quote"] == quote
                    and quote["principal_minor"] == amount
                    and quote["output_minor"] == output
                )
                assert quote["fee_minor"] == 20 and quote["numerator"] == 133 and quote["denominator"] == 100
                for entry, currency, group in (
                    (
                        "source",
                        "USD",
                        [("ATLAS_US:USD", -(amount + 20)), ("STERLING_UK:USD", amount), ("OPERATOR:USD", 20)],
                    ),
                    ("target", "SGD", [("STERLING_UK:SGD", -output), ("MERIDIAN_SG:SGD", output)]),
                ):
                    expected_lines += [
                        {
                            "entry_id": identity + ":" + entry,
                            "line_no": line,
                            "account_id": account,
                            "currency": currency,
                            "issuer": "SHARED",
                            "delta": delta,
                        }
                        for line, (account, delta) in enumerate(group)
                    ]
            assert lines == expected_lines
            independent, replay = independent_audit(store, run), replay_check(store, run)
            assert (
                independent["pass"]
                and replay["match"]
                and replay["event_hash"] == measured["audits"]["replay"]["result"]["event_hash"]
            )
            results.append(
                {
                    "run_id": run,
                    "count": count,
                    "implementation": implementation,
                    "reference_principal_total": sum(amounts),
                    "reference_target_total": sum(outputs),
                    "reference_fees_usd": count * 20,
                    "expected_posted": expected,
                    "observed_positions": positions,
                    "exact_posting_lines_verified": len(lines),
                    "independent": independent,
                    "replay": replay,
                    "pass": True,
                }
            )
    (OUT / "differential.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"matched_worlds": len(results), "reference_balances_and_all_posting_lines": "PASS"}))
    store.engine.dispose()
    source.engine.dispose()


if __name__ == "__main__":
    main()
