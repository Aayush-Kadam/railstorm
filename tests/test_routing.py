import pytest

from railstorm.audit import reconcile
from railstorm.domain import DomainError
from railstorm.routing import Intent, Network, Policy, Router
from railstorm.routing_experiments import SCENARIOS, experiment


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_rt01_through_rt06(store, scenario):
    r = experiment(store, scenario)
    e = r["record"]["evaluation"]
    c = {x["rail"]: x for x in e["candidates"]}
    assert r["routing_replay"]["match"]
    assert all(a["financial_errors"] == 0 for a in r["audits"].values())
    if scenario == "RT-01":
        assert e["selected"] == "INSTANT_LINK"
        assert c["ATOMIC_PVP"]["status"] == "FEASIBLE"
        assert c["INSTANT_LINK"]["status"] == "FEASIBLE"
    if scenario == "RT-02":
        assert c["INSTANT_LINK"]["status"] == "STRUCTURALLY_INELIGIBLE"
    if scenario == "RT-03":
        assert e["selected"] == "ATOMIC_PVP"
        assert c["INSTANT_LINK"]["status"] == "INSUFFICIENTLY_FUNDED"
        assert any(x.get("shortfall_minor") == 13200 for x in c["INSTANT_LINK"]["reasons"])
    if scenario == "RT-04":
        assert any(x["code"] == "INTERMEDIARY_DOWN" for x in c["CORRESPONDENT"]["reasons"])
    if scenario == "RT-05":
        assert e["selected"] is None
    if scenario == "RT-06":
        assert r["record"]["bound_rail"] == "INSTANT_LINK"
        assert r["outcome"]["payment"]["source_final"]
        assert not r["outcome"]["payment"]["destination_final"]
        assert r["outcome"]["payment"]["obligation"]["amount_minor"] == 13300
    router = Router(store)
    a = router.enumerate(
        Intent.model_validate(e["intent"]), Network.model_validate(e["network"]), 30, Policy()
    )
    b = router.enumerate(
        Intent.model_validate(e["intent"]), Network.model_validate(e["network"]), 30, Policy()
    )
    assert a == b


def test_bad_intent_and_finality(store):
    with pytest.raises(ValueError):
        Intent.model_validate({"payment_id": "x", "idempotency_key": "x", "amount_minor": 1.5})
    r = experiment(store, "RT-03")
    router = Router(store)
    e = r["record"]["evaluation"]
    intent = Intent.model_validate(e["intent"]).model_copy(update={"finality": "ATOMIC_REQUIRED"})
    candidates = router.enumerate(intent, Network.model_validate(e["network"]), 0, Policy())["candidates"]
    assert all(c["status"] != "FEASIBLE" for c in candidates if c["rail"] != "ATOMIC_PVP")


def test_atomic_execution_revalidates_and_replays(store):
    r = experiment(store, "RT-03")
    router = Router(store)
    outcome = router.execute(r["control_run"], "payment", 0)
    assert outcome["bound_rail"] == "ATOMIC_PVP"
    assert outcome["payment"]["economic_status"] == "SETTLED"
    again = router.execute(r["control_run"], "payment", 0)
    assert again["payment"] == outcome["payment"]
    assert reconcile(store, outcome["world"])["financial_errors"] == 0


def test_quote_expiry_revalidation_prevents_execution(store):
    r = experiment(store, "RT-03")
    router = Router(store)
    with pytest.raises(DomainError, match="PREFLIGHT_CHANGED"):
        router.execute(r["control_run"], "payment", 30000)
    assert router.read(r["control_run"], "payment")["bound_rail"] is None


def test_routing_api_and_status(store):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        r = client.post("/api/v1/routing/experiments/RT-06").json()
        base = "/api/v1/routing/" + r["control_run"] + "/payment"
        assert client.get(base).json()["bound_rail"] == "INSTANT_LINK"
        assert client.get(base + "/status").json()["payment"]["source_final"]
        assert (
            client.post(base + "/recover", json={"at_ms": 30}).json()["verification"]["financial_errors"] == 0
        )
        assert client.get(base + "/status").json()["payment"]["destination_final"]
        assert client.get(base + "/verification").json()["routing"]["financial_errors"] == 0


def test_idempotency_alias_and_current_liquidity_shortage(store):
    r = experiment(store, "RT-03")
    e = r["record"]["evaluation"]
    router = Router(store)
    intent = Intent.model_validate(e["intent"]).model_copy(update={"payment_id": "alias"})
    with pytest.raises(DomainError, match="IDEMPOTENCY_KEY_REUSED"):
        router.evaluate(r["control_run"], intent, Network.model_validate(e["network"]))
    world = e["network"]["worlds"]["ATOMIC_PVP"]
    from railstorm.domain import PaymentCommand
    from railstorm.ledger import Ledger

    Ledger(store).prepare(
        world, PaymentCommand(idempotency_key="competing", principal_minor=10000, requested_rail="ATOMIC_PVP")
    )
    with pytest.raises(DomainError, match="PREFLIGHT_CHANGED"):
        router.execute(r["control_run"], "payment", 0)


def test_isolated_harness_equivalent_input_manifests(store):
    a = experiment(store, "RT-03", seed=42)
    b = experiment(store, "RT-03", seed=42)
    assert a["control_run"] != b["control_run"]
    assert a["input_hash"] == b["input_hash"]
    assert a["record"]["evaluation"]["candidates"] == b["record"]["evaluation"]["candidates"]
    assert a["record"]["evaluation"]["selected"] == b["record"]["evaluation"]["selected"]


def test_execution_window_is_not_extended_by_late_prepare(store):
    r = experiment(store, "RT-05")
    router = Router(store)
    network = Network.model_validate(r["record"]["evaluation"]["network"]).model_copy(
        update={"quote_available": True}
    )
    intent = Intent(
        payment_id="late", idempotency_key="late", execute_until_ms=100, permitted_rails=("INSTANT_LINK",)
    )
    router.evaluate(r["control_run"], intent, network, 90)
    prepared = router.execute(r["control_run"], "late", 99)
    assert prepared["payment"]["economic_status"] == "PREPARED"
    assert prepared["payment"]["quote"]["expires_ms"] == 100
    expired = router.execute(r["control_run"], "late", 109)
    assert expired["payment"]["economic_status"] == "EXPIRED"
    assert not expired["payment"]["source_final"]
    with store.transaction(expired["world"]) as tx:
        assert not tx.rows("SELECT * FROM journal_entries WHERE run_id=:r AND payment_id='late'")
        assert tx.position("OPERATOR:USD")["posted"] == 0
    assert reconcile(store, expired["world"])["financial_errors"] == 0
