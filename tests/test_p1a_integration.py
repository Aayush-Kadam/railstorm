import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from railstorm.api import create_app
from railstorm.audit import reconcile, replay_check
from railstorm.cb_simulation import CorrespondentScenario, create_correspondent_run
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail, default_relationships
from railstorm.recovery import recover_reservations
from railstorm.simulation import metrics, step


@pytest.mark.parametrize("number", range(1, 11))
def test_correspondent_matrix(store, number):
    name = f"CB-{number:02d}"
    folder = Path("scenarios/correspondent")
    expected = json.loads((folder / "expectations.json").read_text())[name]
    scenario = CorrespondentScenario.model_validate_json((folder / (name + ".json")).read_text())
    run = "matrix-" + uuid.uuid4().hex
    create_correspondent_run(store, run, scenario)
    while not step(store, run, 50)["done"]:
        pass
    report = reconcile(store, run)
    assert report["financial_errors"] == 0, report
    assert replay_check(store, run)["match"]
    m = metrics(store, run)
    assert {c: v["amount_minor"] for c, v in m["exposure_by_currency"].items()} == expected[
        "open_exposure_minor"
    ]
    assert {c: v for c, v in m["fees_by_currency_minor"].items() if v} == expected["fees_minor"]
    client = TestClient(create_app(store))
    detail = client.get(f"/api/v1/runs/{run}/payments/p000000").json()
    chain = detail["chain"]
    assert detail["payment"]["economic_status"] == expected["economic_status"]
    assert sum(h["posting_status"] == "POSTED" for h in chain["hops"]) == expected["posted_stages"]
    assert chain["pending_exposure_minor"] == expected["open_exposure_minor"]
    assert chain["beneficiary_credited"] == (expected["beneficiary_minor"] > 0)
    assert chain["end_to_end_completed"] == (expected["economic_status"] == "SETTLED")
    assert chain["event_count"] >= len(chain["events"])
    assert all(h["event_ids"] and h["instruction_status"] == "ACCEPTED" for h in chain["hops"])
    if expected["beneficiary_minor"]:
        with store.transaction(run) as tx:
            assert (
                tx.position(f"BENEFICIARY:p000000:{scenario.destination_currency}")["posted"]
                == expected["beneficiary_minor"]
            )
    network = client.get(f"/api/v1/runs/{run}/network").json()
    assert network["implemented_rails"] == ["CORRESPONDENT"]
    assert all("issuer" in p for p in network["positions"])


def test_api_chain_actions_and_duplicate_delivery(store):
    client = TestClient(create_app(store))
    sid = "api-cb-" + uuid.uuid4().hex
    assert client.post("/api/v1/correspondent/scenarios", json={"scenario_id": sid}).status_code == 201
    run = client.post("/api/v1/runs", json={"scenario_id": sid}).json()["id"]
    base = f"/api/v1/runs/{run}"
    assert client.post(base + "/chains", json={"idempotency_key": "manual"}).status_code == 201
    for at in (100, 150, 200, 250, 300, 350):
        assert client.post(base + "/chains/manual/advance", json={"at_ms": at}).status_code == 200
    before = client.get(base + "/payments/manual").json()
    assert before["chain"]["end_to_end_completed"]
    for _ in range(3):
        client.post(base + "/chains/manual/advance", json={"at_ms": 400, "hop_index": 0})
    after = client.get(base + "/payments/manual").json()
    assert after["journal"] == before["journal"]
    assert after["events"] == before["events"]
    assert client.post(base + "/chains/manual/return", json={"at_ms": 450}).status_code == 409


def test_process_exit_after_hop_preserves_obligation(store):
    run = "process-" + uuid.uuid4().hex
    rail = CorrespondentRail(store)
    rail.initialize(run)
    rail.create(run, ChainCommand(idempotency_key="crash"))
    code = (
        "from railstorm.store import Store; from railstorm.rails.correspondent import CorrespondentRail; import os; CorrespondentRail(Store()).advance("
        + repr(run)
        + ", 'crash', 100); os._exit(23)"
    )
    child = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), timeout=30)
    assert child.returncode == 23
    with store.transaction(run) as tx:
        assert tx.payment("crash")["next_stage"] == 1
        assert tx.one("SELECT count(*) n FROM cb_obligations WHERE run_id=:r AND status='OPEN'")["n"] == 1
        assert tx.one("SELECT count(*) n FROM reservations WHERE run_id=:r AND status='ACTIVE'")["n"] == 0
    assert recover_reservations(store, run, 101)["actions"] == []
    for at in range(150, 501, 50):
        rail.advance(run, "crash", at)
    assert reconcile(store, run)["financial_errors"] == 0
    assert replay_check(store, run)["match"]


def test_aggregate_exposure_limit_blocks_second_chain(store):
    relationships = default_relationships()
    relationships[0] = relationships[0].model_copy(update={"exposure_limit_minor": 15000})
    run = uuid.uuid4().hex
    rail = CorrespondentRail(store)
    rail.initialize(run, relationships)
    for key in ("one", "two"):
        rail.create(run, ChainCommand(idempotency_key=key))
    assert rail.advance(run, "one", 100)["next_stage"] == 1
    assert rail.advance(run, "two", 100)["reason"] == "AGGREGATE_EXPOSURE_LIMIT"
    assert reconcile(store, run)["financial_errors"] == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"holidays": [0]}, "OPERATING_WINDOW_CLOSED"),
        ({"open_ms": 500}, "OPERATING_WINDOW_CLOSED"),
        ({"status": "DOWN"}, "RELATIONSHIP_DOWN"),
    ],
)
def test_relationship_operating_constraints(store, change, reason):
    relationships = default_relationships()
    relationships[0] = relationships[0].model_copy(update=change)
    run = uuid.uuid4().hex
    rail = CorrespondentRail(store)
    rail.initialize(run, relationships)
    rail.create(run, ChainCommand(idempotency_key="window"))
    assert rail.advance(run, "window", 100)["reason"] == reason
    assert reconcile(store, run)["financial_errors"] == 0
