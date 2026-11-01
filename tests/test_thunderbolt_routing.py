"""Public routing API multicurrency and version-specific preflight contracts."""

import uuid

import pytest
from fastapi.testclient import TestClient

from railstorm.api import factory
from railstorm.audit import reconcile, replay_check
from railstorm.domain import Currency, DomainError
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.routing import Intent, Network, Router


@pytest.mark.parametrize(
    "source,target,sender,receiver,n,d",
    [
        ("USD", "SGD", "ATLAS_US", "MERIDIAN_SG", 133, 100),
        ("USD", "EUR", "ATLAS_US", "RHINE_EU", 92, 100),
        ("USD", "INR", "ATLAS_US", "BHARAT_IN", 83, 1),
        ("EUR", "SGD", "RHINE_EU", "MERIDIAN_SG", 145, 100),
    ],
)
def test_public_multicurrency_routing(store, source, target, sender, receiver, n, d):
    world = "thunder-route-" + uuid.uuid4().hex
    Ledger(store).initialize(
        world,
        {
            "fee_minor": 25,
            "rates": {f"{source}/{target}": [n, d]},
            "funding": {f"{sender}:{source}": 100000, f"STERLING_UK:{target}": 2000000},
        },
    )
    with TestClient(factory()) as client:
        response = client.post(
            "/api/v1/routing/evaluations",
            json={
                "intent": {
                    "payment_id": "p",
                    "idempotency_key": "p",
                    "source": sender,
                    "destination": receiver,
                    "source_currency": source,
                    "destination_currency": target,
                    "amount_minor": 10001,
                },
                "network": {"worlds": {"ATOMIC_PVP": world}},
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["record"]["evaluation"]["selected"] == "ATOMIC_PVP"
        run = body["run_id"]
        url = f"/api/v1/routing/{run}/p/execute"
        first = client.post(url, json={"at_ms": 1})
        assert first.status_code == 200, first.text
        assert first.json()["payment"]["quote"]["output_minor"] == 10001 * n // d
        assert client.post(url, json={"at_ms": 2}).json()["payment"] == first.json()["payment"]
        Ledger(store).acknowledge(world, "p")
        verification = client.get(f"/api/v1/routing/{run}/p/verification").json()
        assert (
            verification["routing"]["financial_errors"] == verification["settlement"]["financial_errors"] == 0
        )
    with store.transaction(world) as tx:
        assert tx.position(f"OPERATOR:{source}")["posted"] == 25
        assert len(tx.rows("SELECT id FROM journal_entries WHERE run_id=:r AND payment_id='p'")) == 2
    assert independent_audit(store, world)["pass"]
    assert reconcile(store, world)["financial_errors"] == 0
    assert replay_check(store, world)["match"] and replay_check(store, run)["match"]


@pytest.mark.parametrize("pair,funding", [("USD/EUR", 0), ("USD/INR", 100000)])
def test_unfunded_or_unsupported_route_rejects(store, pair, funding):
    world, run = "negative-" + uuid.uuid4().hex, "control-" + uuid.uuid4().hex
    Ledger(store).initialize(world, {"rates": {"USD/EUR": [92, 100]}, "funding": {"ATLAS_US:USD": funding}})
    router = Router(store)
    router.initialize(run)
    intent = Intent(
        payment_id="p",
        idempotency_key="p",
        destination="RHINE_EU",
        destination_currency=Currency(pair.split("/")[1]),
    )
    record = router.evaluate(run, intent, Network(worlds={"ATOMIC_PVP": world}))
    assert record["evaluation"]["selected"] is None
    with pytest.raises(DomainError):
        router.execute(run, "p")
    assert independent_audit(store, world)["pass"] and replay_check(store, run)["match"]


def test_scheduled_quote_change_requires_new_routing_decision(store):
    world, run = "quote-" + uuid.uuid4().hex, "control-" + uuid.uuid4().hex
    Ledger(store).initialize(
        world,
        {
            "fee_minor": 25,
            "quote_schedule": [
                {
                    "id": "v1",
                    "version": 1,
                    "pair": "USD/SGD",
                    "numerator": 133,
                    "denominator": 100,
                    "valid_from_ms": 0,
                    "expires_ms": 10,
                },
                {
                    "id": "v2",
                    "version": 2,
                    "pair": "USD/SGD",
                    "numerator": 140,
                    "denominator": 100,
                    "valid_from_ms": 10,
                    "expires_ms": 20,
                },
            ],
        },
    )
    router = Router(store)
    router.initialize(run)
    network = Network(worlds={"ATOMIC_PVP": world})
    record = router.evaluate(run, Intent(payment_id="old", idempotency_key="old"), network)
    candidate = next(c for c in record["evaluation"]["candidates"] if c["rail"] == "ATOMIC_PVP")
    assert candidate["quote_identity"]["version"] == 1
    with pytest.raises(DomainError, match="EXECUTION_QUOTE_CHANGED"):
        router.execute(run, "old", 10)
    router.evaluate(run, Intent(payment_id="new", idempotency_key="new"), network, 10)
    settled = router.execute(run, "new", 11)["payment"]
    assert settled["quote"]["version"] == 2 and settled["quote"]["output_minor"] == 14000
    Ledger(store).acknowledge(world, "new")
    assert independent_audit(store, world)["pass"] and replay_check(store, world)["match"]
