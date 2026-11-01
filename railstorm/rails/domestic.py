"""Synthetic domestic-system eligibility and clearing contract for INSTANT_LINK."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from railstorm.domain import Currency, DomainError

DAY_MS = 86_400_000


class DomesticSystem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    currency: Currency
    participants: tuple[str, ...]
    payment_types: tuple[str, ...] = ("CREDIT_TRANSFER",)
    minimum_minor: StrictInt = Field(default=1, ge=1)
    maximum_minor: StrictInt = Field(default=100_000, ge=1)
    fee_minor: StrictInt = Field(default=1, ge=0)
    available: bool = True
    open_ms: StrictInt = Field(default=0, ge=0, lt=DAY_MS)
    close_ms: StrictInt = Field(default=DAY_MS, gt=0, le=DAY_MS)
    clearing_delay_ms: StrictInt = Field(default=10, ge=1)
    settlement_delay_ms: StrictInt = Field(default=10, ge=1)
    queue_limit: StrictInt = Field(default=100, ge=1)
    timeout_ms: StrictInt = Field(default=5_000, ge=1)
    settlement_policy: str = "PREFUNDED_GROSS"
    finality_rule: str = "COMMITTED_DOMESTIC_JOURNAL"

    @model_validator(mode="after")
    def valid(self):
        if self.minimum_minor > self.maximum_minor or self.open_ms >= self.close_ms:
            raise ValueError("INVALID_SYSTEM_BOUNDS")
        if not self.participants or len(set(self.participants)) != len(self.participants):
            raise ValueError("INVALID_PARTICIPANTS")
        if self.settlement_policy != "PREFUNDED_GROSS":
            raise ValueError("UNSUPPORTED_SETTLEMENT_POLICY")
        if self.finality_rule != "COMMITTED_DOMESTIC_JOURNAL":
            raise ValueError("UNSUPPORTED_FINALITY_RULE")
        return self

    def validate_instruction(
        self, sender: str, receiver: str, currency: Currency, amount: int, at_ms: int
    ) -> None:
        if type(amount) is not int or type(at_ms) is not int or at_ms < 0:
            raise DomainError("INVALID_DOMESTIC_INSTRUCTION")
        if currency != self.currency:
            raise DomainError("DOMESTIC_CURRENCY_MISMATCH")
        if sender not in self.participants or receiver not in self.participants or sender == receiver:
            raise DomainError("DOMESTIC_PARTICIPANT_MISMATCH")
        if not self.minimum_minor <= amount <= self.maximum_minor:
            raise DomainError("DOMESTIC_AMOUNT_INELIGIBLE")
        if not self.available or not self.open_ms <= at_ms % DAY_MS < self.close_ms:
            raise DomainError("DOMESTIC_SYSTEM_UNAVAILABLE")


def default_systems() -> tuple[DomesticSystem, DomesticSystem]:
    return (
        DomesticSystem(id="US_FAST", currency=Currency.USD, participants=("ATLAS_US", "STERLING_UK")),
        DomesticSystem(id="SG_FAST", currency=Currency.SGD, participants=("STERLING_UK", "MERIDIAN_SG")),
    )
