import uuid

from hypothesis import given
from hypothesis import strategies as st

from railstorm.audit import replay_check, state_digest
from railstorm.domain import digest
from railstorm.scenarios import Scenario
from railstorm.simulation import run_scenario
from railstorm.store import Transaction


@given(st.dictionaries(st.text(), st.lists(st.one_of(st.integers(), st.text(), st.none(), st.booleans()))))
def test_streamed_canonical_hash_is_byte_identical(value):
    assert state_digest(value) == digest(value)


def test_replay_checks_events_beyond_first_page(store, monkeypatch):
    run = uuid.uuid4().hex
    result = run_scenario(
        store, run, Scenario(scenario_id="paged", payment_count=50, principal_minor=10000, fee_minor=20)
    )
    assert result["verification"]["replay"]["events"] > 512
    original = Transaction.rows
    sizes = []

    def corrupt(self, sql, **params):
        rows = original(self, sql, **params)
        if "seq>:after" in sql:
            sizes.append(len(rows))
            for row in rows:
                if row["seq"] == 513:
                    row["event_hash"] = "0" * 64
        return rows

    monkeypatch.setattr(Transaction, "rows", corrupt)
    report = replay_check(store, run)
    assert not report["match"]
    assert report["earliest_corrupt_event"] == 513
    assert max(sizes) <= 512
