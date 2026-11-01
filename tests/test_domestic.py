import pytest

from railstorm.domain import Currency, DomainError
from railstorm.rails.domestic import default_systems


def test_domestic_eligibility_and_independent_currency_domains():
    us, sg = default_systems()
    us.validate_instruction("ATLAS_US", "STERLING_UK", Currency.USD, 100, 0)
    sg.validate_instruction("STERLING_UK", "MERIDIAN_SG", Currency.SGD, 133, 20)
    assert us.id != sg.id
    with pytest.raises(DomainError, match="CURRENCY_MISMATCH"):
        sg.validate_instruction("STERLING_UK", "MERIDIAN_SG", Currency.USD, 100, 0)


@pytest.mark.parametrize("amount", [0, -1, 100001, 500000000, True])
def test_domestic_retail_limits(amount):
    us, _ = default_systems()
    with pytest.raises(DomainError):
        us.validate_instruction("ATLAS_US", "STERLING_UK", Currency.USD, amount, 0)


def test_domestic_availability_and_schedule():
    us, _ = default_systems()
    for system in (us.model_copy(update={"available": False}), us.model_copy(update={"open_ms": 100})):
        with pytest.raises(DomainError, match="SYSTEM_UNAVAILABLE"):
            system.validate_instruction("ATLAS_US", "STERLING_UK", Currency.USD, 100, 0)


def test_domestic_participant_and_validation_bounds():
    us, _ = default_systems()
    with pytest.raises(DomainError, match="PARTICIPANT_MISMATCH"):
        us.validate_instruction("MERIDIAN_SG", "STERLING_UK", Currency.USD, 100, 0)
    with pytest.raises(ValueError):
        type(us)(id="invalid", currency=Currency.USD, participants=("a",), minimum_minor=5, maximum_minor=1)
