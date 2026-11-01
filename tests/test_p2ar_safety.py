"""P2-A-R real engine contention, process crash, and supported ordering checks."""

from experiments.p2ar_contention import exercise as contention
from experiments.p2ar_crash import exercise as crash
from experiments.p2ar_ordering import exercise as ordering
from railstorm.store import Store


def test_simultaneous_liquidity_contention():
    result = contention(Store())
    assert result["winner"] != result["loser"]
    assert result["final_source"]["posted"] == 4975
    assert result["independent_audit"]["pass"]


def test_worker_process_crash_during_recovery():
    result = crash()
    assert result["worker_exit_code"] == 73
    assert result["after_recovery"]["independent"]["pass"]


def test_supported_message_orderings():
    result = ordering(Store())
    assert result["final_journal_groups"] == 2
    assert result["final_beneficiary_minor"] == 13300
