"""Small isolated routing experiments, not a policy efficacy comparison."""

import uuid

from railstorm.audit import reconcile, replay_check
from railstorm.domain import digest
from railstorm.ledger import Ledger
from railstorm.rails.correspondent import CorrespondentRail
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Policy, Rail, Router

SCENARIOS = {
    "RT-01": "healthy",
    "RT-02": "instant cap",
    "RT-03": "prefunding shortage",
    "RT-04": "intermediary unavailable",
    "RT-05": "quote unavailable",
    "RT-06": "source final commitment",
}


def experiment(store, scenario, seed=20261030):
    if scenario not in SCENARIOS:
        raise ValueError("UNKNOWN_EXPERIMENT")
    prefix = "rt-" + uuid.uuid4().hex
    worlds: dict[Rail, str] = {
        r: prefix + "-" + r.lower() for r in ("ATOMIC_PVP", "CORRESPONDENT", "INSTANT_LINK")
    }
    Ledger(store).initialize(
        worlds["ATOMIC_PVP"],
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
    CorrespondentRail(store).initialize(worlds["CORRESPONDENT"])
    InstantLink(store).initialize(worlds["INSTANT_LINK"], provider_sgd=100 if scenario == "RT-03" else 50000)
    opening = {}
    for rail_id, world_id in worlds.items():
        with store.transaction(world_id) as tx:
            opening[rail_id] = tx.rows(
                "SELECT account_id,posted,reserved,buffer FROM positions WHERE run_id=:r ORDER BY account_id"
            )
    router = Router(store)
    router.initialize(prefix)
    intent = Intent(
        payment_id="payment",
        idempotency_key="payment",
        amount_minor=500000000 if scenario == "RT-02" else 10000,
    )
    network = Network(
        worlds=worlds,
        unavailable=("RHINE_EU",) if scenario == "RT-04" else (),
        quote_available=scenario != "RT-05",
    )
    router.evaluate(prefix, intent, network, 0, Policy())
    outcome = None
    if scenario in ("RT-01", "RT-06"):
        outcome = router.execute(prefix, "payment", 0)
        if scenario == "RT-06":
            # Retry the same bound payment; no re-enumeration or fallback after source finality.
            outcome = router.execute(prefix, "payment", 10)
        else:
            InstantLink(store).deliver(worlds["INSTANT_LINK"], "payment", "DESTINATION", 30)
    audits = {r: reconcile(store, w) for r, w in worlds.items()}
    assert all(a["financial_errors"] == 0 for a in audits.values())
    logical_manifest = {
        "seed": seed,
        "network_version": network.version,
        "policy": Policy().model_dump(mode="json"),
        "scenario": scenario,
        "intent": intent.model_dump(mode="json"),
        "fault_schedule": [
            {"at_ms": 0, "unavailable": network.unavailable, "quote_available": network.quote_available}
        ],
        "initial_positions": opening,
        "initial_configs": {},
    }
    for r, w in worlds.items():
        with store.transaction(w) as tx:
            logical_manifest["initial_configs"][r] = tx.run["config"]
    return {
        "scenario": scenario,
        "control_run": prefix,
        "manifest": logical_manifest,
        "input_hash": digest(logical_manifest),
        "record": router.read(prefix, "payment"),
        "outcome": outcome,
        "audits": audits,
        "routing_replay": replay_check(store, prefix),
    }
