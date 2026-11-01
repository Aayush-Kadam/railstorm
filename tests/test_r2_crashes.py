"""Actual subprocess exits in disposable databases on both sides of posting."""

import pytest

from experiments.p2ar_crash import exercise


@pytest.mark.parametrize("before", [True, False])
def test_recovery_process_boundaries(before):
    result = exercise(before_posting=before, artifact_name=f"THUNDERBOLT_R2_crash_{before}.json")
    assert result["worker_exit_code"] == (74 if before else 73)
    assert result["after_process_exit"]["independent"]["pass"]
    assert result["after_recovery"]["beneficiary_posted_minor"] == 13300
    assert len(result["after_recovery"]["journals"]) == 2
