"""Strict, bounded, non-executable synthetic scenario input."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from railstorm.domain import BANKS, MAX_MINOR


class Fault(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    type: Literal[
        "LIQUIDITY_FREEZE",
        "PARTICIPANT_DOWN",
        "FX_RATE_SHOCK",
        "MESSAGE_DROP",
        "DUPLICATE_DELIVERY",
        "CRASH_AFTER_COMMIT",
    ]
    at_ms: StrictInt = Field(default=0, ge=0, le=3_600_000)
    duration_ms: StrictInt = Field(default=1000, gt=0, le=3_600_000)
    target: str = "STERLING_UK"
    currency: Literal["USD", "EUR", "SGD", "INR"] = "SGD"
    amount_minor: StrictInt = Field(default=0, ge=0, le=MAX_MINOR)
    numerator: StrictInt = Field(default=120, gt=0, le=100000)
    denominator: StrictInt = Field(default=100, gt=0, le=100000)
    count: StrictInt = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def known_target(self):
        if self.target not in BANKS:
            raise ValueError("UNKNOWN_BANK")
        if self.at_ms + self.duration_ms > 3_600_000:
            raise ValueError("FAULT_DURATION_LIMIT")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    scenario_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    seed: StrictInt = Field(default=20261029, ge=0, le=2**32 - 1)
    payment_count: StrictInt = Field(default=1, ge=1, le=10_000)
    interval_ms: StrictInt = Field(default=10, ge=1, le=100_000)
    settlement_delay_ms: StrictInt = Field(default=50, ge=0, le=100_000)
    principal_minor: StrictInt = Field(default=500_000_000, gt=0, le=MAX_MINOR)
    jitter_minor: StrictInt = Field(default=0, ge=0, le=100_000_000)
    deadline_ms: StrictInt = Field(default=60000, gt=0, le=3_600_000)
    quote_ttl_ms: StrictInt = Field(default=30000, gt=0, le=3_600_000)
    fee_minor: StrictInt = Field(default=20000, ge=0, le=1_000_000)
    routing_policy: Literal["COST_FIRST", "LIQUIDITY_AWARE"] = "COST_FIRST"
    funding: dict[str, StrictInt] = Field(default_factory=dict, max_length=20)
    faults: list[Fault] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounds(self):
        if (
            self.interval_ms * (self.payment_count - 1)
            + max(self.deadline_ms, self.settlement_delay_ms + 1000)
            > 3_600_000
        ):
            raise ValueError("VIRTUAL_DURATION_LIMIT")
        if self.jitter_minor >= self.principal_minor:
            raise ValueError("JITTER_EXCEEDS_PRINCIPAL")
        if len({fault.id for fault in self.faults}) != len(self.faults):
            raise ValueError("DUPLICATE_FAULT_ID")
        for key, value in self.funding.items():
            parts = key.split(":")
            if (
                len(parts) != 2
                or parts[0] not in BANKS
                or parts[1] not in ("USD", "EUR", "SGD", "INR")
                or not 0 <= value <= MAX_MINOR
            ):
                raise ValueError("INVALID_FUNDING")
        return self
