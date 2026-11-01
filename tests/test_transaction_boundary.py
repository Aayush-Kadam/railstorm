import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from railstorm.audit import replay_check
from railstorm.scenarios import Scenario
from railstorm.simulation import create_run, step


def test_batch_failure_rolls_back_finance_and_cursor(store):
    run = uuid.uuid4().hex
    create_run(
        store, run, Scenario(scenario_id="batch", payment_count=2, principal_minor=10000, fee_minor=20)
    )
    before = replay_check(store, run)
    with store.engine.begin() as conn:
        conn.execute(
            text("""CREATE OR REPLACE FUNCTION test_cursor_failure() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.cursor=2 THEN RAISE EXCEPTION 'INJECTED_CURSOR_CRASH'; END IF; RETURN NEW; END; $$""")
        )
        conn.execute(
            text(
                "CREATE TRIGGER test_cursor BEFORE UPDATE ON runtime FOR EACH ROW EXECUTE FUNCTION test_cursor_failure()"
            )
        )
    try:
        with pytest.raises(DBAPIError, match="INJECTED_CURSOR_CRASH"):
            step(store, run, 2)
    finally:
        with store.engine.begin() as conn:
            conn.execute(text("DROP TRIGGER test_cursor ON runtime"))
            conn.execute(text("DROP FUNCTION test_cursor_failure()"))
    assert replay_check(store, run)["state_hash"] == before["state_hash"]
    with store.transaction(run) as tx:
        assert tx.one("SELECT cursor FROM runtime WHERE run_id=:r")["cursor"] == 0
    assert not step(store, run, 2)["done"]


def test_journal_cannot_gain_lines_after_commit(world):
    ledger, run = world
    with pytest.raises(DBAPIError, match="JOURNAL_ALREADY_SEALED"):
        with ledger.store.transaction(run) as tx:
            tx.execute("""INSERT INTO journal_lines VALUES
              (:r,'opening:ATLAS_US:USD',2,'ATLAS_US:USD','USD','SHARED',1)""")
