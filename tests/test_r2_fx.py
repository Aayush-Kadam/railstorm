"""Native scheduled FX exact arithmetic, repricing liquidity and restart evidence."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from railstorm.audit import reconcile, replay_check, snapshot
from railstorm.domain import Currency, PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit


def record(store, run, name):
    with store.transaction(run) as tx:
        state = snapshot(tx)
    value = {
        "run_id": run,
        "state": state,
        "native": reconcile(store, run),
        "independent": independent_audit(store, run),
        "replay": replay_check(store, run),
    }
    assert (
        value["native"]["financial_errors"] == 0 and value["independent"]["pass"] and value["replay"]["match"]
    )
    out = Path(__file__).resolve().parents[1] / "docs/thunderbolt_r2_evidence"
    out.mkdir(exist_ok=True)
    (out / (name + ".json")).write_text(json.dumps(value, indent=2))
    return value


@pytest.mark.parametrize(
    "source,target,sender,receiver,n,d",
    [
        (Currency.USD, Currency.SGD, "ATLAS_US", "MERIDIAN_SG", 133, 100),
        (Currency.USD, Currency.EUR, "ATLAS_US", "RHINE_EU", 92, 100),
        (Currency.USD, Currency.INR, "ATLAS_US", "BHARAT_IN", 83, 1),
        (Currency.EUR, Currency.SGD, "RHINE_EU", "MERIDIAN_SG", 145, 100),
    ],
)
def test_rounding_fees_restart_and_historical_versions(store, source, target, sender, receiver, n, d):
    run = "r2-fx-" + uuid.uuid4().hex
    schedule = [
        {
            "id": "old",
            "version": 1,
            "pair": f"{source}/{target}",
            "numerator": n,
            "denominator": d,
            "valid_from_ms": 0,
            "expires_ms": 10,
        },
        {
            "id": "new",
            "version": 2,
            "pair": f"{source}/{target}",
            "numerator": n + 1,
            "denominator": d,
            "valid_from_ms": 10,
            "expires_ms": 20,
        },
    ]
    ledger = Ledger(store)
    ledger.initialize(
        run,
        {
            "fee_minor": 25,
            "quote_schedule": schedule,
            "funding": {f"{sender}:{source}": 100000, f"STERLING_UK:{target}": 2000000},
        },
    )
    opening = record(store, run, f"FX-{source}-{target}-opening")
    expected = 0
    for index, amount in enumerate((3, 101, 10001)):
        key = f"p{index}"
        command = PaymentCommand(
            idempotency_key=key,
            sender_bank_id=sender,
            receiver_bank_id=receiver,
            source_currency=source,
            destination_currency=target,
            principal_minor=amount,
        )
        quote = ledger.prepare(run, command, 1)["quote"]
        assert quote["version"] == 1 and quote["output_minor"] == amount * n // d
        record(store, run, f"FX-{source}-{target}-{key}-prepared")
        ledger = Ledger(store)
        ledger.settle(run, key, 1)
        ledger.acknowledge(run, key)
        expected += amount * n // d
    prior = record(store, run, f"FX-{source}-{target}-old-final")
    new = command.model_copy(update={"idempotency_key": "new"})
    assert ledger.prepare(run, new, 10)["quote"]["version"] == 2
    ledger.settle(run, "new", 11)
    ledger.acknowledge(run, "new")
    final = record(store, run, f"FX-{source}-{target}-new-final")
    for key in ("p0", "p1", "p2"):
        assert final["state"]["payments"][key] == prior["state"]["payments"][key]
    assert final["state"]["positions"][f"OPERATOR:{source}"]["posted"] == 100
    initial = opening["state"]["positions"][f"{receiver}:{target}"]["posted"]
    assert (
        final["state"]["positions"][f"{receiver}:{target}"]["posted"]
        == initial + expected + 10001 * (n + 1) // d
    )


def test_repricing_shortage_releases_no_extra_reservations(store):
    run = "r2-short-" + uuid.uuid4().hex
    ledger = Ledger(store)
    ledger.initialize(
        run,
        {
            "fee_minor": 25,
            "funding": {"STERLING_UK:SGD": 14000},
            "quote_schedule": [
                {
                    "id": "old",
                    "version": 1,
                    "pair": "USD/SGD",
                    "numerator": 133,
                    "denominator": 100,
                    "valid_from_ms": 0,
                    "expires_ms": 10,
                },
                {
                    "id": "new",
                    "version": 2,
                    "pair": "USD/SGD",
                    "numerator": 150,
                    "denominator": 100,
                    "valid_from_ms": 10,
                    "expires_ms": 20,
                },
            ],
        },
    )
    command = PaymentCommand(idempotency_key="old", principal_minor=10000)
    ledger.prepare(run, command)
    record(store, run, "FX-shortage-prepared")
    assert ledger.settle(run, "old", 10)["economic_status"] == "EXPIRED"
    assert (
        ledger.prepare(run, command.model_copy(update={"idempotency_key": "new"}), 10)["economic_status"]
        == "REJECTED"
    )
    final = record(store, run, "FX-shortage-final")
    assert all(p["reserved"] == 0 for p in final["state"]["positions"].values())
    assert not any(j["payment_id"] for j in final["state"]["journals"].values())


def test_concurrent_execution_uses_selected_immutable_version(store):
    run = "r2-concurrent-" + uuid.uuid4().hex
    Ledger(store).initialize(
        run,
        {
            "fee_minor": 25,
            "funding": {"ATLAS_US:USD": 15000},
            "quote_schedule": [
                {
                    "id": "version2",
                    "version": 2,
                    "pair": "USD/SGD",
                    "numerator": 140,
                    "denominator": 100,
                    "valid_from_ms": 10,
                    "expires_ms": 20,
                }
            ],
        },
    )
    record(store, run, "FX-concurrent-opening")

    def execute(key):
        from railstorm.store import Store

        worker = Store()
        try:
            return Ledger(worker).submit(run, PaymentCommand(idempotency_key=key, principal_minor=10000), 10)
        finally:
            worker.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["a", "b"]))
    assert sorted(p["economic_status"] for p in results) == ["REJECTED", "SETTLED"]
    assert next(p for p in results if p["economic_status"] == "SETTLED")["quote"]["version"] == 2
    record(store, run, "FX-concurrent-final")
