import uuid

import pytest

from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.instant_fixtures import IL_FIXTURES
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store


def world(store, **kwargs):
    run = "c2-" + uuid.uuid4().hex
    rail = InstantLink(store)
    rail.initialize(run, **kwargs)
    return rail, run


def oracle(store, run):
    report = reconcile(store, run)
    assert report["financial_errors"] == 0, report
    assert replay_check(store, run)["match"]
    with store.transaction(run) as tx:
        for currency, total in [("USD", 20000), ("SGD", tx.run["config"]["funding"]["STERLING_UK:SGD"])]:
            rows = tx.rows(
                "SELECT p.*,a.kind FROM positions p JOIN accounts a ON a.run_id=p.run_id AND a.id=p.account_id WHERE p.run_id=:r AND a.currency=:c",
                c=currency,
            )
            assert sum(r["posted"] for r in rows if r["kind"] != "OPENING_OFFSET") == total
            assert all(r["posted"] >= r["reserved"] + r["buffer"] for r in rows if r["kind"] == "CLAIM")
    return report


@pytest.mark.parametrize("fixture", IL_FIXTURES)
def test_all_original_il_fixtures(store, fixture):
    rail, run = world(
        store,
        provider_sgd=100 if fixture == "IL-03" else 20000 if fixture == "IL-12" else 50000,
        source_available=fixture != "IL-04",
    )
    amount = 500000000 if fixture == "IL-02" else 3 if fixture == "IL-11" else 10000
    cmd = PaymentCommand(idempotency_key="p", principal_minor=amount, requested_rail="INSTANT_LINK")
    before = rail.prepare(run, cmd)
    if fixture in ("IL-02", "IL-03", "IL-04"):
        assert before["economic_status"] == "REJECTED"
        assert not rail.inspect(run, "p")["journals"] and not rail.inspect(run, "p")["reservations"]
        oracle(store, run)
        return
    if fixture == "IL-08":
        rail.deliver(run, "p", "SOURCE", 30000)
        assert rail.inspect(run, "p")["payment"]["economic_status"] == "EXPIRED"
        assert rail.recoverable(run) == []
        assert all(r["status"] == "RELEASED" for r in rail.inspect(run, "p")["reservations"])
        oracle(store, run)
        return
    if fixture == "IL-09":
        # Freeze all unreserved inventory after preparation. Reserved funds remain binding.
        with store.transaction(run) as tx:
            tx.execute("UPDATE positions SET buffer=36700 WHERE run_id=:r AND account_id='STERLING_UK:SGD'")
            tx.event("BUFFER_CHANGED", "STERLING_UK:SGD", {"account_id": "STERLING_UK:SGD", "buffer": 36700})
        # Existing generic buffer event schema is checked separately below.
    rail.deliver(run, "p", "SOURCE", 10)
    oracle(store, run)
    if fixture in ("IL-05", "IL-10", "IL-12"):
        rail.deliver(run, "p", "DESTINATION", 30, available=False)
        pending = rail.inspect(run, "p")
        assert pending["payment"]["obligation"]["amount_minor"] == 13300
        assert pending["payment"]["source_final"] and not pending["payment"]["destination_final"]
        assert (
            next(r for r in pending["reservations"] if r["account_id"] == "STERLING_UK:SGD")["status"]
            == "ACTIVE"
        )
        oracle(store, run)
        restarted = InstantLink(Store())
        restarted.recover(run, "p", 70000)
        restarted.store.engine.dispose()
    else:
        rail.deliver(run, "p", "DESTINATION", 30, lose_ack=fixture == "IL-06")
        if fixture == "IL-06":
            assert any(m["ack_status"] == "LOST" for m in rail.messages(run, "p"))
            InstantLink(store).recover(run, "p", 40)
    if fixture == "IL-07":
        rail.prepare(run, cmd)
        rail.deliver(run, "p", "DESTINATION", 40)
        rail.deliver(run, "p", "SOURCE", 40)
    assert rail.inspect(run, "p")["payment"]["phase"] == "COMPLETED"
    detail = rail.inspect(run, "p")
    assert len(detail["journals"]) == 2
    positions = {p["account_id"]: p for p in detail["positions"]}
    assert positions["MERIDIAN_SG:SGD"]["posted"] == amount * 133 // 100
    assert positions["OPERATOR:USD"]["posted"] == 25
    assert all(r["status"] == "CONSUMED" for r in detail["reservations"])
    oracle(store, run)


def test_message_tamper_and_unknown_recovery_fail_closed(store):
    rail, run = world(store)
    cmd = PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    rail.prepare(run, cmd)
    with store.transaction(run) as tx:
        tx.execute("UPDATE il_messages SET data=jsonb_set(data,'{destination}','\"WRONG\"') WHERE run_id=:r")
    assert reconcile(store, run)["financial_errors"] > 0
    with pytest.raises(DomainError, match="RECONCILIATION"):
        rail.recover(run, "p", 10)


def test_restart_sent_before_business_execution(store):
    rail, run = world(store)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    with store.transaction(run) as tx:
        msg = rail.messages(run, "p")[0]
        msg.update(delivery_status="SENT", ack_status="PENDING")
        rail._save_message(tx, msg)
    restarted = InstantLink(store)
    restarted.recover(run, "p", 10)
    restarted.recover(run, "p", 30)
    restarted.recover(run, "p", 40)
    assert len(restarted.inspect(run, "p")["journals"]) == 2
    oracle(store, run)


def test_adverse_rate_after_binding_and_reconciliation_corruption(store, monkeypatch):
    from railstorm.ledger import RATES

    rail, run = world(store)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    rail.deliver(run, "p", "SOURCE", 10)
    monkeypatch.setitem(RATES, "USD/SGD", [1, 1])
    rail.deliver(run, "p", "DESTINATION", 30)
    assert rail.inspect(run, "p")["payment"]["quote"]["output_minor"] == 13300
    oracle(store, run)
    with store.transaction(run) as tx:
        tx.execute(
            "UPDATE payments SET data=jsonb_set(data,'{obligation}',jsonb_build_object('amount_minor',13300)) WHERE run_id=:r AND id='p'"
        )
    assert reconcile(store, run)["financial_errors"] > 0
    assert not replay_check(store, run)["match"]


def test_api_recovery_invalid_inputs_and_restart(store):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        r = client.post("/api/v1/instant-link/golden").json()
        run = r["run_id"]
        base = f"/api/v1/instant-link/{run}/golden"
        assert client.post(base + "/deliver/SOURCE", json={"at_ms": 10}).status_code == 200
        assert (
            client.post(base + "/deliver/DESTINATION", json={"at_ms": 30, "available": False}).status_code
            == 200
        )
        assert client.post(base + "/deliver/INVALID", json={"at_ms": 31}).status_code == 409
        assert client.post(base + "/recover", json={"at_ms": -1}).status_code == 422
    with TestClient(create_app(store)) as client:
        assert client.get(base).json()["payment"]["obligation"]["amount_minor"] == 13300
        assert (
            client.post(base + "/recover", json={"at_ms": 40}).json()["verification"]["financial_errors"] == 0
        )
        assert client.post(base + "/recover", json={"at_ms": 41}).status_code == 200
        assert len(client.get(base).json()["journals"]) == 2
        cmd = {"idempotency_key": "golden", "principal_minor": 10000, "requested_rail": "INSTANT_LINK"}
        assert client.post(f"/api/v1/instant-link/{run}/submit", json={"command": cmd}).status_code == 200
        assert (
            client.post(
                "/api/v1/instant-link/payments", json={"command": {"principal_minor": -1}}
            ).status_code
            == 422
        )


def test_source_journal_outbox_failure_rolls_back_whole_stage(store, monkeypatch):
    rail, run = world(store)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    original = rail._message

    def fail(tx, payment, stage, at_ms):
        if stage == "DESTINATION":
            raise RuntimeError("CRASH_BEFORE_OUTBOX")
        return original(tx, payment, stage, at_ms)

    monkeypatch.setattr(rail, "_message", fail)
    with pytest.raises(RuntimeError, match="CRASH_BEFORE_OUTBOX"):
        rail.deliver(run, "p", "SOURCE", 10)
    detail = rail.inspect(run, "p")
    assert not detail["journals"] and not detail["payment"]["source_final"]
    assert all(r["status"] == "ACTIVE" for r in detail["reservations"])
    oracle(store, run)
    monkeypatch.setattr(rail, "_message", original)
    rail.recover(run, "p", 11)
    rail.recover(run, "p", 31)
    assert rail.inspect(run, "p")["payment"]["destination_final"]
    oracle(store, run)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_posting",
        "duplicate_posting",
        "orphan_hold",
        "completed_obligation",
        "pending_conflict",
        "credit_without_source",
    ],
)
def test_independent_corruption_oracle(store, corruption):
    from copy import deepcopy

    from railstorm.audit import snapshot
    from railstorm.instant_audit import instant_findings

    rail, run = world(store)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    rail.deliver(run, "p", "SOURCE", 10)
    rail.deliver(run, "p", "DESTINATION", 30)
    with store.transaction(run) as tx:
        state = deepcopy(snapshot(tx))
        records = tx.rows("SELECT * FROM payments WHERE run_id=:r")
    if corruption == "missing_posting":
        del state["journals"]["p:us-final"]
    if corruption == "duplicate_posting":
        state["journals"]["duplicate"] = deepcopy(state["journals"]["p:sg-final"])
    if corruption == "orphan_hold":
        state["reservations"]["extra"] = {"payment_id": "p", "status": "ACTIVE"}
    if corruption == "completed_obligation":
        state["payments"]["p"]["obligation"] = {"amount_minor": 13300}
    if corruption == "pending_conflict":
        state["payments"]["p"]["destination_final"] = False
    if corruption == "credit_without_source":
        state["payments"]["p"]["source_final"] = False
    assert instant_findings(state, records)


@pytest.mark.parametrize("fixture", IL_FIXTURES)
def test_fixture_api_reads_live_records(store, fixture):
    from fastapi.testclient import TestClient

    from railstorm.api import create_app

    with TestClient(create_app(store)) as client:
        result = client.post("/api/v1/instant-link/fixtures/" + fixture)
        assert result.status_code == 200
        data = result.json()
        assert data["verification"]["financial_errors"] == 0
        live = client.get("/api/v1/instant-link/" + data["run_id"] + "/fixture").json()
        assert live["payment"] == data["inspection"]["payment"]
