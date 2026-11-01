"""P2-A auditor checks against disposable native runs."""

import json
import uuid

from sqlalchemy import text

from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink


def test_chaos_api_persists_actual_run(store):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        registry = client.get("/api/v1/chaos/scenarios")
        assert registry.status_code == 200 and "CH-04" in registry.json()
        created = client.post("/api/v1/chaos/scenarios/CH-04/runs", json={"seed": 17, "count": 1})
        assert created.status_code == 200, created.text
        body = client.get("/api/v1/chaos/runs/" + created.json()["id"])
        assert body.status_code == 200, body.text
        value = body.json()
        assert value["audit"] and all(x["independent"]["pass"] for x in value["audit"].values())
        assert any(stage["action"] == "pending_audit" for stage in value["stages"])
        assert value["final"]["payment"]["destination_final"]


def test_independent_oracle_detects_position_corruption(store):
    run = "p2a-test-" + uuid.uuid4().hex
    Ledger(store).initialize(run)
    assert independent_audit(store, run)["pass"]
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE positions SET posted=posted+1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
            {"r": run},
        )
    try:
        report = independent_audit(store, run)
        assert not report["pass"]
        assert "POSITION_PROJECTION" in {f["check"] for f in report["findings"]}
    finally:
        with store.engine.begin() as conn:
            conn.execute(
                text("UPDATE positions SET posted=posted-1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
                {"r": run},
            )
    assert independent_audit(store, run)["pass"]


def test_independent_oracle_detects_false_instant_completion(store):
    run = "p2a-test-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    rail.deliver(run, "p", "SOURCE", 10)
    assert independent_audit(store, run)["pass"]
    with store.engine.connect() as conn:
        original = conn.execute(
            text("SELECT data FROM payments WHERE run_id=:r AND id='p'"), {"r": run}
        ).scalar_one()
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE payments SET data=jsonb_set(data,'{economic_status}','\"SETTLED\"') WHERE run_id=:r AND id='p'"
            ),
            {"r": run},
        )
    try:
        report = independent_audit(store, run)
        assert not report["pass"]
        assert "INSTANT_FINALITY" in {f["check"] for f in report["findings"]}
    finally:
        with store.engine.begin() as conn:
            conn.execute(
                text("UPDATE payments SET data=CAST(:d AS jsonb) WHERE run_id=:r AND id='p'"),
                {"r": run, "d": json.dumps(original)},
            )
