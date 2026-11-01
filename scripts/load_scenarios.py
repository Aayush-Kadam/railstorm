"""Idempotently register built-in immutable scenario fixtures."""

import json
from pathlib import Path

from sqlalchemy import text

from railstorm.cb_simulation import CorrespondentScenario
from railstorm.domain import canonical, digest
from railstorm.scenarios import Scenario
from railstorm.store import Store

store = Store()
with store.engine.begin() as conn:
    paths = [
        *sorted(Path("scenarios").glob("*.json")),
        *sorted(Path("scenarios/correspondent").glob("CB-*.json")),
    ]
    for path in paths:
        scenario = (
            CorrespondentScenario.model_validate_json(path.read_text())
            if path.parent.name == "correspondent"
            else Scenario.model_validate_json(path.read_text())
        )
        config = scenario.model_dump(mode="json")
        result = conn.execute(
            text("SELECT fingerprint FROM scenarios WHERE id=:id"), {"id": scenario.scenario_id}
        ).first()
        if result and result[0] != digest(config):
            raise RuntimeError("Existing scenario fingerprint differs: " + scenario.scenario_id)
        if not result:
            conn.execute(
                text("INSERT INTO scenarios VALUES(:id,CAST(:c AS jsonb),:f)"),
                {"id": scenario.scenario_id, "c": canonical(config), "f": digest(config)},
            )
        print(json.dumps({"scenario": scenario.scenario_id, "fingerprint": digest(config)}))
