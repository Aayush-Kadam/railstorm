"""Live routed-correspondent examples for simulator operators."""

import uuid

from railstorm.domain import Currency, DomainError
from railstorm.ledger import Ledger
from railstorm.rails.correspondent import CorrespondentRail, default_relationships
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Policy, Router
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault


def example(store, case):
    if case not in {"CB-R01", "CB-R05", "CB-R06"}:
        raise DomainError("EXAMPLE_NOT_FOUND")
    run = "cbr-example-" + uuid.uuid4().hex
    cb = run + "-cb"
    atomic = run + "-atomic"
    instant = run + "-instant"
    rels = default_relationships()
    if case == "CB-R05":
        rels = [r.model_copy(update={"variable_fee_bps": 7}) if r.id == "SR_SGD" else r for r in rels]
    CorrespondentRail(store).initialize(cb, rels)
    Ledger(store).initialize(
        atomic,
        {
            "fee_minor": 25,
            "funding": {
                "ATLAS_US:USD": 20000,
                "STERLING_UK:USD": 0,
                "STERLING_UK:SGD": 50000,
                "MERIDIAN_SG:SGD": 0,
            },
        },
    )
    InstantLink(store).initialize(instant)
    router = Router(store)
    router.initialize(run)
    intent = Intent(
        payment_id="payment",
        idempotency_key="payment",
        destination_currency=Currency.SGD if case == "CB-R05" else Currency.USD,
        maximum_fees_by_currency={Currency.USD: 1000, Currency.SGD: 1000},
    )
    network = Network(worlds={"ATOMIC_PVP": atomic, "CORRESPONDENT": cb, "INSTANT_LINK": instant})
    router.evaluate(
        run, intent, network, 0, Policy(preference=("CORRESPONDENT", "ATOMIC_PVP", "INSTANT_LINK"))
    )
    router.execute(run, "payment", 0)
    if case == "CB-R06":
        router.recover(run, "payment", 100)
        router.recover(run, "payment", 150)
        apply_fault(
            store,
            cb,
            Fault(id="example-down", type="PARTICIPANT_DOWN", target="RHINE_EU", at_ms=151, duration_ms=649),
            True,
            151,
        )
        router.recover(run, "payment", 200)
    else:
        for t in range(100, 1100, 100):
            outcome = router.recover(run, "payment", t)
            if (
                outcome["payment"]["economic_status"] == "SETTLED"
                and outcome["payment"]["ack_status"] == "ACKNOWLEDGED"
            ):
                break
    return {
        "control_run": run,
        "record": router.read(run, "payment"),
        "status": router.status(run, "payment"),
        "verification": router.reconcile(run, "payment"),
    }


def restore(store, run, payment, at_ms):
    router = Router(store)
    status = router.status(run, payment)
    if status["record"]["bound_rail"] != "CORRESPONDENT":
        raise DomainError("WRONG_BOUND_RAIL")
    with store.transaction(status["world"]) as tx:
        rows = tx.rows("SELECT data FROM faults WHERE run_id=:r AND active=true")
    for row in rows:
        fault = Fault.model_validate(row["data"])
        if fault.type == "PARTICIPANT_DOWN":
            apply_fault(store, status["world"], fault, False, at_ms)
    return router.status(run, payment)
