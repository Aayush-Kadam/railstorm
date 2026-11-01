from concurrent.futures import ThreadPoolExecutor

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.recovery import recover_reservations
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault


def test_abandoned_preparation_expires_during_outage(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="abandoned"))
    apply_fault(
        ledger.store, run, Fault(id="down", type="PARTICIPANT_DOWN", at_ms=1, duration_ms=60000), True, 1
    )
    report = recover_reservations(ledger.store, run, 30000)
    assert report["errors"] == 0
    assert report["actions"][0]["action"] == "EXPIRED"
    with ledger.store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD")["posted"] == 1_000_000_000
        assert tx.position("STERLING_UK:SGD")["posted"] == 1_000_000_000
        assert tx.position("ATLAS_US:USD")["reserved"] == 0
        assert tx.position("STERLING_UK:SGD")["reserved"] == 0
    before = replay_check(ledger.store, run)
    assert recover_reservations(ledger.store, run, 30000)["actions"] == []
    assert ledger.settle(run, "abandoned", 30001)["economic_status"] == "EXPIRED"
    assert replay_check(ledger.store, run) == before
    assert reconcile(ledger.store, run)["financial_errors"] == 0


def test_recovery_never_aborts_durable_commit(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="decision"))
    # Deliberately inconsistent durable state: real shared-book commit is atomic.
    # Recovery must report it rather than treating timeout as permission to abort.
    with ledger.store.transaction(run) as tx:
        tx.decision("decision", "COMMIT")
    report = recover_reservations(ledger.store, run, 30000)
    assert report["actions"][0]["action"] == "BLOCKED_COMMIT"
    with ledger.store.transaction(run) as tx:
        assert tx.position("ATLAS_US:USD")["reserved"] == 500020000
        assert tx.position("STERLING_UK:SGD")["reserved"] == 665000000
        assert tx.payment("decision")["economic_status"] == "PREPARED"
    assert reconcile(ledger.store, run)["financial_errors"] > 0


def test_recovery_after_commit_missing_ack_posts_nothing(world):
    ledger, run = world
    ledger.submit(run, PaymentCommand(idempotency_key="committed"), drop_ack=True)
    before = replay_check(ledger.store, run)
    assert recover_reservations(ledger.store, run, 60000)["actions"] == []
    assert replay_check(ledger.store, run) == before
    ledger.acknowledge(run, "committed")
    assert reconcile(ledger.store, run)["findings"] == []


def test_contested_duplicate_timeout_release(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="contested-timeout"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: recover_reservations(ledger.store, run, 30000), range(2)))
    assert all(r["errors"] == 0 for r in reports)
    with ledger.store.transaction(run) as tx:
        assert tx.one("SELECT count(*) n FROM reservations WHERE run_id=:r AND status='RELEASED'")["n"] == 2
        assert tx.position("ATLAS_US:USD")["reserved"] == 0
    assert reconcile(ledger.store, run)["financial_errors"] == 0


def test_unexpired_hold_is_not_released(world):
    ledger, run = world
    ledger.prepare(run, PaymentCommand(idempotency_key="live"))
    assert recover_reservations(ledger.store, run, 29999)["actions"] == []
    ledger.settle(run, "live", 29999)
    assert reconcile(ledger.store, run)["financial_errors"] == 0
