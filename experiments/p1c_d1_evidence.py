"""Execute independent CB-R financial fixture assertions and preserve durable projections."""

import importlib.util
import json
from pathlib import Path

import railstorm.routing_experiments as experiments
from railstorm.audit import empty_state, reconcile, reduce_event, replay_check, snapshot
from railstorm.domain import digest
from railstorm.store import Store

spec = importlib.util.spec_from_file_location("cb_r_tests", "tests/test_correspondent_routing.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = Store()
original_setup = module.setup_case
original_checks = module.checks
original_experiment = experiments.experiment
results = {}
for case in module.CASES:
    report = {"samples": [], "isolated_inputs": []}
    results[case] = report

    def setup(s, key):
        value = original_setup(s, key)
        router, run, world, intent, network, record = value
        with s.transaction(world) as tx:
            opening = tx.rows(
                "SELECT account_id,posted,reserved,buffer FROM positions WHERE run_id=:r ORDER BY account_id"
            )
            config = tx.run["config"]
        report["initial"] = {
            "control_run": run,
            "world": world,
            "intent": intent.model_dump(mode="json"),
            "network": network.model_dump(mode="json"),
            "record": record,
            "opening_positions": opening,
            "config": config,
        }
        return value

    def checks(s, run, world):
        original_checks(s, run, world)
        with s.transaction(world) as tx:
            projection = snapshot(tx)
        report["samples"].append(
            {
                "control_run": run,
                "world": world,
                "routing_replay": replay_check(s, run),
                "settlement_replay": replay_check(s, world),
                "reconciliation": reconcile(s, world),
                "projection": projection,
            }
        )

    def experiment(s, scenario, seed=20261030):
        r = original_experiment(s, scenario, seed)
        report["isolated_inputs"].append(r)
        return r

    module.setup_case = setup
    module.checks = checks
    experiments.experiment = experiment
    module.test_correspondent_routing_matrix(store, case)
    report["result"] = "PASS"
experiments.experiment = original_experiment
Path("docs/P1C_D1_observed.json").write_text(json.dumps(results, indent=2, default=str))
print("CB-R01–CB-R12 passed with recorded financial projections and routing/settlement replay hashes")

# Preserve exact audited event prefixes, including intermediate pending samples.

for report in results.values():
    for sample in report["samples"]:
        for scope, world, replay in [
            ("financial", sample["world"], sample["settlement_replay"]),
            ("routing", sample["control_run"], sample["routing_replay"]),
        ]:
            history = []
            while len(history) < replay["events"]:
                page = store.events(
                    world, history[-1]["seq"] if history else 0, min(512, replay["events"] - len(history))
                )
                assert page
                history.extend(page)
            assert history[-1]["event_hash"] == replay["event_hash"]
            projection = empty_state()
            for event in history:
                reduce_event(projection, event)
            assert digest(projection) == replay["replay_hash"]
            sample[scope + "_events"] = history
            if scope == "routing":
                sample["routing_projection"] = projection
Path("docs/P1C_D1_observed.json").write_text(json.dumps(results, indent=2, default=str))
