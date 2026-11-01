import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from railstorm.api import create_app
from railstorm.audit import reconcile, replay_check
from railstorm.domain import DomainError, PaymentCommand
from railstorm.rails.instant_link import InstantLink


def setup(store, **funding):
    rail = InstantLink(store)
    run = "il-test-" + uuid.uuid4().hex
    rail.initialize(run, **funding)
    cmd = PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    return rail, run, cmd


def balances(store, run):
    with store.transaction(run) as tx:
        return {r["account_id"]: r["posted"] for r in tx.rows("SELECT * FROM positions WHERE run_id=:r")}


def verify(store, run):
    report = reconcile(store, run)
    assert report["financial_errors"] == 0, report
    assert replay_check(store, run)["match"]


def test_il01_and_il07_exact_settlement_and_duplicate_messages(store):
    rail, run, cmd = setup(store)
    assert rail.prepare(run, cmd)["phase"] == "FUNDS_RESERVED"
    assert rail.prepare(run, cmd)["phase"] == "FUNDS_RESERVED"
    assert rail.advance(run, "p", 10)["source_final"]
    assert rail.advance(run, "p", 10)["phase"] != "COMPLETED"
    assert rail.advance(run, "p", 30, drop_ack=True)["phase"] == "COMPLETED"
    rail.advance(run, "p", 40)
    rail.prepare(run, cmd, 40)
    b = balances(store, run)
    assert [
        b[a]
        for a in ["ATLAS_US:USD", "STERLING_UK:USD", "STERLING_UK:SGD", "MERIDIAN_SG:SGD", "OPERATOR:USD"]
    ] == [9975, 10000, 36700, 13300, 25]
    assert sum(v for k, v in b.items() if k.endswith(":USD") and not k.startswith("OPENING:")) == 20000
    assert sum(v for k, v in b.items() if k.endswith(":SGD") and not k.startswith("OPENING:")) == 50000
    assert len(rail.inspect(run, "p")["journals"]) == 2
    verify(store, run)


def test_il05_outage_preserves_final_source_and_recovers_after_deadline(store):
    rail, run, cmd = setup(store)
    rail.prepare(run, cmd)
    rail.advance(run, "p", 10)
    s = rail.advance(run, "p", 40, destination_available=False)
    assert not s["destination_final"] and s["obligation"]["amount_minor"] == 13300
    assert s["reason"] == "SG_FAST_UNAVAILABLE"
    b = balances(store, run)
    assert b["ATLAS_US:USD"] == 9975 and b["STERLING_UK:USD"] == 10000
    assert b["STERLING_UK:SGD"] == 50000 and b["MERIDIAN_SG:SGD"] == 0
    verify(store, run)
    from railstorm.recovery import recover_reservations

    actions = recover_reservations(store, run, 70000)["actions"]
    assert actions[0]["action"] == "BLOCKED_COMMIT"
    assert rail.advance(run, "p", 70001)["phase"] == "COMPLETED"
    verify(store, run)


@pytest.mark.parametrize(
    "amount,sender,provider", [(500000000, 20000, 50000), (10000, 10024, 50000), (10000, 20000, 13299)]
)
def test_il02_and_insufficient_funding_no_payment_journal(store, amount, sender, provider):
    rail, run, cmd = setup(store, sender_usd=sender, provider_sgd=provider)
    cmd = cmd.model_copy(update={"principal_minor": amount})
    assert rail.prepare(run, cmd)["economic_status"] == "REJECTED"
    assert not rail.inspect(run, "p")["journals"]
    verify(store, run)


def test_expired_quote_releases_both_holds(store):
    rail, run, cmd = setup(store)
    rail.prepare(run, cmd)
    assert rail.advance(run, "p", 30000)["economic_status"] == "EXPIRED"
    assert all(r["status"] == "RELEASED" for r in rail.inspect(run, "p")["reservations"])
    verify(store, run)


def test_changed_duplicate_command_rejected(store):
    rail, run, cmd = setup(store)
    rail.prepare(run, cmd)
    with pytest.raises(DomainError, match="IDEMPOTENCY_KEY_REUSED"):
        rail.prepare(run, cmd.model_copy(update={"principal_minor": 9999}))


def test_destination_database_failure_rolls_back_and_retries(store):
    rail, run, cmd = setup(store)
    rail.prepare(run, cmd)
    rail.advance(run, "p", 10)
    with store.transaction(run) as tx:
        tx.execute(
            "CREATE OR REPLACE FUNCTION il_test_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'IL_INJECTED'; END $$"
        )
        tx.execute(
            "CREATE TRIGGER il_test_failure BEFORE INSERT ON journal_entries FOR EACH ROW WHEN (right(NEW.id,8) = 'sg-final') EXECUTE FUNCTION il_test_fail()"
        )
    try:
        with pytest.raises(Exception, match="IL_INJECTED"):
            rail.advance(run, "p", 30)
    finally:
        with store.engine.begin() as conn:
            conn.execute(text("DROP TRIGGER il_test_failure ON journal_entries"))
            conn.execute(text("DROP FUNCTION il_test_fail()"))
    assert rail.inspect(run, "p")["payment"]["obligation"]["amount_minor"] == 13300
    verify(store, run)
    assert rail.advance(run, "p", 30)["phase"] == "COMPLETED"
    verify(store, run)


def test_minimal_api_golden_and_verification(store):
    with TestClient(create_app(store)) as client:
        result = client.post("/api/v1/instant-link/golden").json()
        path = f"/api/v1/instant-link/{result['run_id']}/golden"
        assert client.get(path).status_code == 200
        assert client.post(path + "/advance?at_ms=10").json()["source_final"]
        assert client.post(path + "/advance?at_ms=30").json()["phase"] == "COMPLETED"
        assert client.get(path + "/verification").json()["financial_errors"] == 0


def test_event_order_issuer_and_insufficient_shared_liquidity(store):
    from concurrent.futures import ThreadPoolExecutor

    rail, run, cmd = setup(store)
    other = cmd.model_copy(update={"idempotency_key": "other"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: rail.prepare(run, c), [cmd, other]))
    assert sorted(s["economic_status"] for s in results) == ["PREPARED", "REJECTED"]
    accepted = next(s["id"] for s in results if s["economic_status"] == "PREPARED")
    rail.advance(run, accepted, 10)
    rail.advance(run, accepted, 30)
    events = store.events(run, 0, 1000)
    kinds = [e["event_type"] for e in events]
    assert kinds.index("IL_SOURCE_FINAL") < kinds.index("IL_DESTINATION_FINAL")
    journals = [
        e["payload"] for e in events if e["event_type"] == "JOURNAL_POSTED" and e["payload"]["payment_id"]
    ]
    assert [j["issuer"] for j in journals] == ["US_FAST", "SG_FAST"]
    verify(store, run)
