"""Execute original twelve financial fixture oracles and preserve durable replay evidence."""

import importlib.util
import json
from pathlib import Path

from railstorm.audit import replay_check
from railstorm.instant_fixtures import IL_FIXTURES
from railstorm.store import Store

spec = importlib.util.spec_from_file_location("c2tests", "tests/test_instant_c2.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = Store()
original_world = module.world
original_oracle = module.oracle
reports = {}
for fixture in IL_FIXTURES:
    samples = []

    def world(instance, **kwargs):
        rail, run = original_world(instance, **kwargs)
        reports[fixture] = {"name": IL_FIXTURES[fixture], "run_id": run, "samples": samples}
        return rail, run

    def oracle(instance, run):
        result = original_oracle(instance, run)
        samples.append({"reconciliation": result, "replay": replay_check(instance, run)})
        return result

    module.world = world
    module.oracle = oracle
    module.test_all_original_il_fixtures(store, fixture)
    reports[fixture]["result"] = "PASS"
    reports[fixture]["inspection"] = module.InstantLink(store).inspect(reports[fixture]["run_id"], "p")
Path("docs/P1B_C2_fixture_observed.json").write_text(json.dumps(reports, indent=2, default=str))
print("12 original fixtures passed with saved financial/replay evidence")
