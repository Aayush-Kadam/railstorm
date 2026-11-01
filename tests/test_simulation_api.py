import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from railstorm.api import create_app
from railstorm.audit import reconcile, replay_check
from railstorm.scenarios import Fault, Scenario
from railstorm.simulation import run_scenario


@pytest.mark.parametrize(
    "fault",
    [
        "LIQUIDITY_FREEZE",
        "PARTICIPANT_DOWN",
        "FX_RATE_SHOCK",
        "MESSAGE_DROP",
        "DUPLICATE_DELIVERY",
        "CRASH_AFTER_COMMIT",
    ],
)
def test_six_fault_classes(store, fault):
    scenario = Scenario(
        scenario_id=fault,
        payment_count=5,
        principal_minor=10000,
        fee_minor=20,
        faults=[Fault(id="f", type=fault, at_ms=0, duration_ms=100, amount_minor=999_999_999)],
    )
    result = run_scenario(store, uuid.uuid4().hex, scenario)
    assert result["verification"]["financial_errors"] == 0
    assert result["verification"]["replay"]["match"]
    if fault == "PARTICIPANT_DOWN":
        assert result["metrics"]["settled"] == 0
    if fault in ("MESSAGE_DROP", "CRASH_AFTER_COMMIT"):
        assert result["metrics"]["settled"] == 5
        assert result["metrics"]["missing_ack"] == 0


def test_quote_lock_survives_rate_shock(store):
    scenario = Scenario(
        scenario_id="lock",
        principal_minor=500000000,
        faults=[
            Fault(id="shock", type="FX_RATE_SHOCK", at_ms=10, duration_ms=100, numerator=100, denominator=100)
        ],
    )
    run = uuid.uuid4().hex
    run_scenario(store, run, scenario)
    with store.transaction(run) as tx:
        assert tx.payment("p000000")["quote"]["output_minor"] == 665000000


def test_down_after_prepare_recovers_forward(store):
    scenario = Scenario(
        scenario_id="forward", faults=[Fault(id="down", type="PARTICIPANT_DOWN", at_ms=10, duration_ms=100)]
    )
    result = run_scenario(store, uuid.uuid4().hex, scenario)
    assert result["metrics"]["settled"] == 1
    assert result["metrics"]["p95_ms"] == 1050
    assert result["verification"]["financial_errors"] == 0


def test_scenario_rerun_hashes(store):
    scenario = Scenario(
        scenario_id="repeated",
        seed=42,
        payment_count=10,
        principal_minor=10000,
        jitter_minor=100,
        fee_minor=20,
    )
    first = run_scenario(store, uuid.uuid4().hex, scenario)
    second = run_scenario(store, uuid.uuid4().hex, scenario)
    assert first["verification"]["replay"]["event_hash"] == second["verification"]["replay"]["event_hash"]
    assert first["verification"]["replay"]["state_hash"] == second["verification"]["replay"]["state_hash"]


def test_api_full_workflow(store):
    client = TestClient(create_app(store))
    sid = "api-" + uuid.uuid4().hex
    assert client.post("/api/v1/scenarios", json={"scenario_id": sid}).status_code == 201
    run = client.post("/api/v1/runs", json={"scenario_id": sid}).json()["id"]
    base = f"/api/v1/runs/{run}"
    assert client.get(base).json()["status"] == "READY"
    assert client.post(base + "/step", json={"count": 1}).json()["cursor"] == 1
    assert client.get(base + "/payments/p000000").json()["payment"]["economic_status"] == "PREPARED"
    assert client.post(base + "/step", json={"count": 3}).json()["done"]
    assert client.get(base + "/metrics").json()["settled"] == 1
    assert client.post(base + "/replay-check").json()["match"]
    assert client.post(base + "/reconcile").json()["financial_errors"] == 0
    with client.websocket_connect(base + "/stream") as ws:
        batch = ws.receive_json()
        assert batch["events"][0]["seq"] == 1
        assert batch["cursor"] > 0
    assert client.get(base + "/events?limit=501").status_code == 422
    assert client.get(base + "/export").json()["verification"]["replay"]["match"]


def test_api_injection_and_invalid_input(store):
    client = TestClient(create_app(store))
    sid = "inject-" + uuid.uuid4().hex
    client.post("/api/v1/scenarios", json={"scenario_id": sid})
    run = client.post("/api/v1/runs", json={"scenario_id": sid}).json()["id"]
    base = f"/api/v1/runs/{run}"
    fault = {"id": "late", "type": "MESSAGE_DROP", "at_ms": 1, "duration_ms": 100}
    assert client.post(base + "/faults", json=fault).status_code == 200
    assert client.post(base + "/faults", json=fault).status_code == 409
    assert client.post(base + "/step", json={"count": 100}).json()["done"]
    assert client.post(base + "/reconcile").json()["financial_errors"] == 0
    assert (
        client.post("/api/v1/scenarios", json={"scenario_id": "bad", "payment_count": 10001}).status_code
        == 422
    )
    assert (
        client.post(
            base + "/payments", json={"command": {"idempotency_key": "float", "principal_minor": 1.0}}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/runs/no-such-run").status_code == 404


def test_projection_corruption_detected(world):
    ledger, run = world
    with ledger.store.transaction(run) as tx:
        tx.execute("UPDATE positions SET posted=posted+1 WHERE run_id=:r AND account_id='ATLAS_US:USD'")
    assert replay_check(ledger.store, run)["projection_difference"] == "state.positions.ATLAS_US:USD.posted"
    assert reconcile(ledger.store, run)["financial_errors"] >= 1


def test_config_validation():
    with pytest.raises(ValidationError):
        Scenario(scenario_id="bad", payment_count=10000, interval_ms=100000)
    with pytest.raises(ValidationError):
        Scenario(scenario_id="bad", funding={"REAL_BANK:USD": 1})
