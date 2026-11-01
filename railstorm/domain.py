"""Exact money, command contracts and pure deterministic primitives."""

import hashlib
import heapq
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_MINOR = 9_000_000_000_000_000


class DomainError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    SGD = "SGD"
    INR = "INR"


@dataclass(frozen=True)
class Money:
    currency: Currency
    amount_minor: int

    def __post_init__(self):
        if not isinstance(self.currency, Currency):
            raise DomainError("UNSUPPORTED_CURRENCY")
        if type(self.amount_minor) is not int or not 0 <= self.amount_minor <= MAX_MINOR:
            raise DomainError("INVALID_AMOUNT")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def convert(amount: int, numerator: int, denominator: int) -> int:
    """Floor positive rational conversion in target minor units; both scales are 2."""
    if any(type(x) is not int for x in (amount, numerator, denominator)):
        raise DomainError("INVALID_RATE")
    if amount <= 0 or numerator <= 0 or denominator <= 0:
        raise DomainError("INVALID_RATE")
    result = amount * numerator // denominator
    if not 0 < result <= MAX_MINOR:
        raise DomainError("INVALID_OUTPUT")
    return result


BANKS = {
    "ATLAS_US": "Atlas Bank · New York",
    "STERLING_UK": "Sterling Bank · London",
    "RHINE_EU": "Rhine Bank · Frankfurt",
    "MERIDIAN_SG": "Meridian Bank · Singapore",
    "BHARAT_IN": "Bharat Bank · Mumbai",
}

TRANSITIONS = {
    "CREATED": {"CREATED", "PREPARED", "REJECTED"},
    "PREPARED": {"PREPARED", "SETTLED", "EXPIRED", "ABORTED"},
    "SETTLED": {"SETTLED"},
    "REJECTED": set(),
    "EXPIRED": set(),
    "ABORTED": set(),
}


class PaymentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    sender_bank_id: str = "ATLAS_US"
    receiver_bank_id: str = "MERIDIAN_SG"
    source_currency: Currency = Currency.USD
    destination_currency: Currency = Currency.SGD
    principal_minor: StrictInt = Field(default=500_000_000, gt=0, le=MAX_MINOR)
    max_fee_minor: StrictInt = Field(default=25_000, ge=0, le=MAX_MINOR)
    minimum_receive_minor: StrictInt = Field(default=0, ge=0, le=MAX_MINOR)
    deadline_ms: StrictInt = Field(default=60_000, gt=0, le=3_600_000)
    requested_rail: str = Field(
        default="ATOMIC_PVP", pattern="^(ATOMIC_PVP|CORRESPONDENT|INSTANT_LINK|AUTO)$"
    )

    @model_validator(mode="after")
    def banks_exist(self):
        if self.sender_bank_id not in BANKS or self.receiver_bank_id not in BANKS:
            raise ValueError("UNKNOWN_BANK")
        if self.sender_bank_id == self.receiver_bank_id:
            raise ValueError("SAME_BANK")
        return self


class Scheduler:
    """Stable total order; no wall clock participates in financial decisions."""

    def __init__(self):
        self.now = 0
        self.sequence = 0
        self.queue: list[tuple[int, int, int, str, dict]] = []

    def push(self, at_ms: int, priority: int, kind: str, payload: dict):
        if at_ms < self.now or len(self.queue) >= 200_000:
            raise DomainError("INVALID_SCHEDULE")
        self.sequence += 1
        heapq.heappush(self.queue, (at_ms, priority, self.sequence, kind, payload))

    def pop(self):
        item = heapq.heappop(self.queue)
        self.now = item[0]
        return item
