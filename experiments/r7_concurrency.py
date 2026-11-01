"""Diagnostic workers in one financial world, never substituted for the frozen queue."""

import argparse
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"
parser = argparse.ArgumentParser()
parser.add_argument("--implementation", choices=("baseline", "modified"), required=True)
args = parser.parse_args()
if args.implementation == "baseline":
    sys.path.insert(0, str(ROOT / ".work/r7/baseline"))

from sqlalchemy import event  # noqa: E402

from experiments.p2ar_contention import exercise  # noqa: E402
from experiments.r4_profile import resources  # noqa: E402
from railstorm.audit import reconcile, replay_check, snapshot  # noqa: E402
from railstorm.domain import PaymentCommand, convert  # noqa: E402
from railstorm.ledger import Ledger  # noqa: E402
from railstorm.market_quotes import replace  # noqa: E402
from railstorm.p2a_oracle import independent_audit  # noqa: E402
from railstorm.store import Store  # noqa: E402


def verify(store, run):
    native, replay, independent = (
        reconcile(store, run),
        replay_check(store, run),
        independent_audit(store, run),
    )
    assert native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    with store.transaction(run) as tx:
        state = snapshot(tx)
        payments = tx.rows("SELECT id,data FROM payments WHERE run_id=:r ORDER BY id")
        groups = tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id IS NOT NULL")[
            "n"
        ]
    return {
        "native": native,
        "replay": replay,
        "independent": independent,
        "state": state,
        "payments": payments,
        "journal_groups": groups,
    }


def case(store, kind, workers):
    assert resources()["ram_available"] > (1536 if workers == 4 else 768) * 1024**2
    run = "r7-concurrency-" + uuid.uuid4().hex
    sources = ("ATLAS_US", "RHINE_EU", "BHARAT_IN")
    destinations = ("MERIDIAN_SG", "RHINE_EU", "BHARAT_IN")
    currencies = ("USD", "EUR", "SGD", "INR")
    funding = {f"{bank}:USD": 1000000000 for bank in sources}
    funding.update({f"ATLAS_US:{currency}": 1000000000 for currency in currencies})
    if kind == "shortage":
        funding["ATLAS_US:USD"] = 15000
    Ledger(store).initialize(run, {"fee_minor": 20, "funding": funding})
    with store.transaction(run) as tx:
        opening_posted = {
            r["account_id"]: r["posted"]
            for r in tx.rows("SELECT account_id,posted FROM positions WHERE run_id=:r")
        }
    if kind == "quote":
        replace(
            store,
            run,
            pair="USD/SGD",
            version=1,
            numerator=133,
            denominator=100,
            at_ms=0,
            expires_ms=100000,
            fee_minor=20,
        )
    commands = []
    for index in range(12):
        values: dict[str, Any] = {
            "idempotency_key": "same" if kind == "duplicate" else f"p{index:04d}",
            "principal_minor": 10000,
        }
        if kind == "disjoint":
            values.update(source_currency=currencies[index % 4], destination_currency=currencies[index % 4])
        elif kind == "shared_source":
            values["receiver_bank_id"] = destinations[index % len(destinations)]
        elif kind == "shared_destination":
            values["sender_bank_id"] = sources[index % len(sources)]
        commands.append(PaymentCommand(**values))
    start_gate = threading.Barrier(workers + (1 if kind == "quote" else 0))
    acquisition = []
    checkout_peak = []

    def before(conn, cursor, statement, parameters, context, many):
        if "FROM runs WHERE id=" in statement and "FOR UPDATE" in statement:
            context.r7_lock_started = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, many):
        if hasattr(context, "r7_lock_started"):
            acquisition.append(time.perf_counter() - context.r7_lock_started)

    setattr(after, "railstorm_pipeline_safe", True)

    def checkout(dbapi, record, proxy):
        checkout_peak.append(getattr(store.engine.pool, "checkedout")())

    def worker(number):
        start_gate.wait(timeout=15)
        return [Ledger(store).submit(run, command) for command in commands[number::workers]]

    def updater():
        start_gate.wait(timeout=15)
        return replace(
            store,
            run,
            pair="USD/SGD",
            version=2,
            numerator=140,
            denominator=100,
            at_ms=0,
            expires_ms=100000,
            fee_minor=20,
        )

    event.listen(store.engine, "before_cursor_execute", before)
    event.listen(store.engine, "after_cursor_execute", after)
    event.listen(store.engine.pool, "checkout", checkout)
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=workers + (1 if kind == "quote" else 0)) as pool:
            futures = [pool.submit(worker, n) for n in range(workers)]
            quote_future = pool.submit(updater) if kind == "quote" else None
            responses = [future.result(timeout=60) for future in futures]
            if quote_future:
                quote_future.result(timeout=60)
        elapsed = time.perf_counter() - started
    finally:
        event.remove(store.engine, "before_cursor_execute", before)
        event.remove(store.engine, "after_cursor_execute", after)
        event.remove(store.engine.pool, "checkout", checkout)
    financial = verify(store, run)
    settled = [p["data"] for p in financial["payments"] if p["data"]["economic_status"] == "SETTLED"]
    expected = 1 if kind in {"shortage", "duplicate"} else 12
    assert len(settled) == expected
    reference = dict(opening_posted)
    for payment in settled:
        quote = payment["quote"]
        assert quote["output_minor"] == convert(
            quote["principal_minor"], quote["numerator"], quote["denominator"]
        )
        assert quote["fee_minor"] == 20
        if kind == "quote":
            assert quote["version"] in (1, 2)
            assert quote["numerator"] == (133 if quote["version"] == 1 else 140)
        command = payment["command"]
        source, target = quote["source"], quote["target"]
        reference[f"{command['sender_bank_id']}:{source}"] -= quote["principal_minor"] + quote["fee_minor"]
        reference[f"{command['receiver_bank_id']}:{target}"] += quote["output_minor"]
        reference[f"OPERATOR:{source}"] += quote["fee_minor"]
        if source != target:
            reference[f"STERLING_UK:{source}"] += quote["principal_minor"]
            reference[f"STERLING_UK:{target}"] -= quote["output_minor"]
    with store.transaction(run) as tx:
        observed = {
            r["account_id"]: r["posted"]
            for r in tx.rows("SELECT account_id,posted FROM positions WHERE run_id=:r")
        }
        assert observed == reference
        assert tx.one("SELECT count(*) n FROM positions WHERE run_id=:r AND reserved<>0")["n"] == 0
    assert financial["journal_groups"] == expected * (1 if kind == "disjoint" else 2)
    assert sum(p["data"]["economic_status"] == "REJECTED" for p in financial["payments"]) == (
        11 if kind == "shortage" else 0
    )
    return {
        "kind": kind,
        "workers": workers,
        "quote_updater_workers": int(kind == "quote"),
        "attempts": 12,
        "completed_unique": len(settled),
        "elapsed_seconds": elapsed,
        "lock_acquisition_seconds": acquisition,
        "peak_checked_out": max(checkout_peak),
        "financial": financial,
        "independent_reference_posted": reference,
        "responses": responses,
        "retries": 0,
        "errors": 0,
        "scope": "Diagnostic individual native submit calls, separate from historical queue throughput; same12 commands across worker counts, shared run/event-chain mutex retained",
    }


def main():
    path = OUT / ("concurrency-" + args.implementation + ".json")
    assert not path.exists()
    store = Store()
    rows = []
    for kind in ("disjoint", "shared_source", "shared_destination", "shortage", "duplicate", "quote"):
        for workers in (1, 2, 4):
            rows.append(case(store, kind, workers))
            path.with_name(path.stem + "-partial.json").write_text(
                json.dumps(rows, indent=2), encoding="utf-8"
            )
    forced = exercise(store)
    assert max(forced["observed_lock_waits"]) == 2
    path.write_text(
        json.dumps(
            {
                "cases": rows,
                "forced_shortage_waits": forced,
                "eight_workers": "NOT_JUSTIFIED: shared world mutex serializes even disjoint accounts; no acceptance substitution",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"implementation": args.implementation, "cases": len(rows), "safety": "PASS"}))
    store.engine.dispose()


if __name__ == "__main__":
    main()
