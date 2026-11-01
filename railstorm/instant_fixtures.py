"""Original P1B fixture IDs retained; C2 provisional IDs do not rename them."""

IL_FIXTURES = {
    "IL-01": "Healthy",
    "IL-02": "Oversized",
    "IL-03": "Insufficient prefunding",
    "IL-04": "Source unavailable",
    "IL-05": "Destination outage",
    "IL-06": "Lost acknowledgement",
    "IL-07": "Duplicate delivery",
    "IL-08": "Quote expiry",
    "IL-09": "Liquidity shock",
    "IL-10": "Recovery after restoration",
    "IL-11": "Fee and rounding",
    "IL-12": "Compound stress",
}


def execute_fixture(store, fixture):
    """Operator-only deterministic synthetic fixture; all results read durable state."""
    import uuid

    from railstorm.audit import reconcile
    from railstorm.domain import DomainError, PaymentCommand
    from railstorm.rails.instant_link import InstantLink

    if fixture not in IL_FIXTURES:
        raise DomainError("FIXTURE_NOT_FOUND")
    rail = InstantLink(store)
    run = "il-fixture-" + uuid.uuid4().hex
    rail.initialize(
        run,
        provider_sgd=100 if fixture == "IL-03" else 20000 if fixture == "IL-12" else 50000,
        source_available=fixture != "IL-04",
    )
    cmd = PaymentCommand(
        idempotency_key="fixture",
        principal_minor=500000000 if fixture == "IL-02" else 3 if fixture == "IL-11" else 10000,
        requested_rail="INSTANT_LINK",
    )
    state = rail.prepare(run, cmd)
    if state["economic_status"] == "PREPARED":
        if fixture == "IL-09":
            with store.transaction(run) as tx:
                tx.execute(
                    "UPDATE positions SET buffer=36700 WHERE run_id=:r AND account_id='STERLING_UK:SGD'"
                )
                tx.event(
                    "BUFFER_CHANGED", "STERLING_UK:SGD", {"account_id": "STERLING_UK:SGD", "buffer": 36700}
                )
        rail.deliver(run, "fixture", "SOURCE", 30000 if fixture == "IL-08" else 10)
        if fixture != "IL-08":
            rail.deliver(
                run,
                "fixture",
                "DESTINATION",
                300 if fixture == "IL-12" else 30,
                available=fixture not in ("IL-05", "IL-10", "IL-12"),
                lose_ack=fixture == "IL-06",
            )
            if fixture in ("IL-06", "IL-10"):
                InstantLink(store).recover(run, "fixture", 70000)
            if fixture == "IL-07":
                rail.prepare(run, cmd)
                rail.deliver(run, "fixture", "DESTINATION", 40)
    return {
        "run_id": run,
        "payment_id": "fixture",
        "fixture": fixture,
        "inspection": rail.inspect(run, "fixture"),
        "verification": reconcile(store, run),
    }
