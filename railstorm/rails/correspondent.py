"""Bank-local correspondent books and separately committed onward instructions."""

from collections import deque

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from railstorm.domain import BANKS, MAX_MINOR, Currency, DomainError, canonical, convert, digest
from railstorm.ledger import RATES, Ledger
from railstorm.store import Store

DAY = 86_400_000
CB_TRANSITIONS = {
    "CREATED": {"CREATED", "QUEUED", "REJECTED"},
    "QUEUED": {"QUEUED", "IN_PROGRESS", "EXPIRED", "ABORTED"},
    "IN_PROGRESS": {"IN_PROGRESS", "PENDING_EXPOSURE", "SETTLED", "RETURN_REQUESTED"},
    "PENDING_EXPOSURE": {"PENDING_EXPOSURE", "IN_PROGRESS", "SETTLED", "RETURN_REQUESTED"},
    "RETURN_REQUESTED": {"RETURN_REQUESTED", "RETURNED"},
    "RETURNED": set(),
    "EXPIRED": set(),
    "ABORTED": set(),
    "REJECTED": set(),
    "SETTLED": {"SETTLED"},
}


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    respondent: str
    correspondent: str
    currency: Currency
    funding_minor: StrictInt = Field(default=1000000, ge=0, le=MAX_MINOR)
    fixed_fee_minor: StrictInt = Field(default=100, ge=0, le=1000000)
    variable_fee_bps: StrictInt = Field(default=0, ge=0, le=1000)
    charge_to: str = Field(default="BENEFICIARY", pattern="^BENEFICIARY$")
    delay_ms: StrictInt = Field(default=100, ge=1, le=60000)
    open_ms: StrictInt = Field(default=0, ge=0, lt=DAY)
    cutoff_ms: StrictInt = Field(default=DAY, gt=0, le=DAY)
    maximum_minor: StrictInt = Field(default=1000000000, gt=0, le=MAX_MINOR)
    exposure_limit_minor: StrictInt = Field(default=1000000000, gt=0, le=MAX_MINOR)
    status: str = Field(default="ACTIVE", pattern="^(ACTIVE|DOWN)$")
    holidays: list[StrictInt] = Field(default_factory=list, max_length=366)
    credit_minor: StrictInt = Field(default=0, ge=0, le=0)

    @model_validator(mode="after")
    def valid(self):
        if (
            self.respondent not in BANKS
            or self.correspondent not in BANKS
            or self.respondent == self.correspondent
        ):
            raise ValueError("INVALID_RELATIONSHIP_BANKS")
        if self.open_ms >= self.cutoff_ms or any(d < 0 for d in self.holidays):
            raise ValueError("INVALID_WINDOW")
        return self


def default_relationships():
    return [
        Relationship(
            id=i, respondent=a, correspondent=b, currency=Currency(c), fixed_fee_minor=f, funding_minor=n
        )
        for i, a, b, c, f, n in [
            ("AS_USD", "ATLAS_US", "STERLING_UK", "USD", 100, 1000000),
            ("SR_USD", "STERLING_UK", "RHINE_EU", "USD", 50, 1000000),
            ("RM_USD", "RHINE_EU", "MERIDIAN_SG", "USD", 25, 1000000),
            ("SR_SGD", "STERLING_UK", "RHINE_EU", "SGD", 50, 2000000),
            ("RM_SGD", "RHINE_EU", "MERIDIAN_SG", "SGD", 25, 2000000),
        ]
    ]


class ChainCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    sender_bank_id: str = "ATLAS_US"
    receiver_bank_id: str = "MERIDIAN_SG"
    source_currency: Currency = Currency.USD
    destination_currency: Currency = Currency.USD
    principal_minor: StrictInt = Field(default=10000, gt=0, le=MAX_MINOR)
    minimum_receive_minor: StrictInt = Field(default=1, ge=1, le=MAX_MINOR)
    max_fees_minor: dict[Currency, StrictInt] = Field(
        default_factory=lambda: {
            Currency.USD: 10000,
            Currency.SGD: 10000,
            Currency.EUR: 10000,
            Currency.INR: 10000,
        }
    )
    deadline_ms: StrictInt = Field(default=30000, gt=0, le=DAY)

    @model_validator(mode="after")
    def valid(self):
        if (
            self.sender_bank_id not in BANKS
            or self.receiver_bank_id not in BANKS
            or self.sender_bank_id == self.receiver_bank_id
        ):
            raise ValueError("INVALID_BANKS")
        if any(v < 0 or v > MAX_MINOR for v in self.max_fees_minor.values()):
            raise ValueError("INVALID_FEE_LIMIT")
        return self


def cash(bank, currency):
    return f"GL:{bank}:CASH:{currency}"


def equity(bank, role, currency):
    return f"GL:{bank}:{role}:{currency}"


def nostro(rel):
    return f"NOSTRO:{rel.id}"


def vostro(rel):
    return f"VOSTRO:{rel.id}"


class CorrespondentRail:
    def __init__(self, store: Store):
        self.store = store

    def initialize(
        self, run_id: str, relationships: list[Relationship] | None = None, config: dict | None = None
    ):
        relationships = relationships if relationships is not None else default_relationships()
        if len({r.id for r in relationships}) != len(relationships) or len(relationships) > 30:
            raise DomainError("RELATIONSHIP_LIMIT")
        configuration = {
            **(config or {}),
            "rail": "CORRESPONDENT",
            "relationships": [r.model_dump(mode="json") for r in relationships],
        }

        # Opening funding, bank-local books and relationships share one initial
        # transaction; a failed prefunding leg leaves no half-created world.
        def setup(tx):
            for bank in BANKS:
                for currency in Currency:
                    c = str(currency)
                    tx.account(cash(bank, c), bank, c, "ASSET", bank)
                    for role in ("CAPITAL", "PAYMENT_EXPENSE", "FEES", "FX"):
                        tx.account(equity(bank, role, c), bank, c, "EQUITY", bank)
                    amount = tx.position(f"{bank}:{c}")["posted"]
                    if amount:
                        tx.journal(
                            f"cb-capital:{bank}:{c}",
                            None,
                            c,
                            [(cash(bank, c), -amount), (equity(bank, "CAPITAL", c), amount)],
                            issuer=bank,
                        )
            # Directed funding order is explicit, not inferred from reciprocal links.
            for rel in relationships:
                c, a, b, n = str(rel.currency), rel.respondent, rel.correspondent, rel.funding_minor
                tx.account(nostro(rel), a, c, "ASSET", a)
                tx.account(vostro(rel), a, c, "CLAIM", b)
                tx.execute(
                    "INSERT INTO cb_relationships VALUES(:r,:id,:a,:b,:c,:n,:v,CAST(:d AS jsonb))",
                    id=rel.id,
                    a=a,
                    b=b,
                    c=c,
                    n=nostro(rel),
                    v=vostro(rel),
                    d=canonical(rel.model_dump(mode="json")),
                )
                tx.event(
                    "CB_RELATIONSHIP_OPENED",
                    rel.id,
                    {
                        "id": rel.id,
                        "respondent": a,
                        "correspondent": b,
                        "currency": c,
                        "nostro_id": nostro(rel),
                        "vostro_id": vostro(rel),
                        "config": rel.model_dump(mode="json"),
                    },
                )
                if n:
                    tx.journal(f"cb-funding:{rel.id}:cash", None, c, [(f"{a}:{c}", -n), (f"{b}:{c}", n)])
                    tx.journal(
                        f"cb-funding:{rel.id}:respondent",
                        None,
                        c,
                        [(cash(a, c), n), (nostro(rel), -n)],
                        issuer=a,
                    )
                    tx.journal(
                        f"cb-funding:{rel.id}:correspondent",
                        None,
                        c,
                        [(cash(b, c), -n), (vostro(rel), n)],
                        issuer=b,
                    )
            tx.event(
                "CB_WORLD_READY", "world", {"funding": "existing SHARED claims moved; no duplicate issuance"}
            )

        Ledger(self.store).initialize(run_id, configuration, after_funding=setup)

    def _plan(self, relationships, cmd, at_ms):
        source, target = str(cmd.source_currency), str(cmd.destination_currency)
        queue: deque = deque([(cmd.sender_bank_id, source, cmd.principal_minor, [], set(), {}, False)])
        while queue:
            bank, c, amount, path, visited, fees, fx_used = queue.popleft()
            if bank == cmd.receiver_bank_id and c == target and path:
                if amount < cmd.minimum_receive_minor:
                    continue
                return path, amount, fees
            if len(path) >= 6 or (bank, c) in visited:
                continue
            next_visited = visited | {(bank, c)}
            for rel in sorted(relationships, key=lambda r: r.id):
                if rel.respondent != bank or str(rel.currency) != c:
                    continue
                fee = rel.fixed_fee_minor + amount * rel.variable_fee_bps // 10000
                new_fees = {**fees, c: fees.get(c, 0) + fee}
                if (
                    amount > min(rel.maximum_minor, rel.exposure_limit_minor)
                    or fee >= amount
                    or new_fees[c] > cmd.max_fees_minor.get(Currency(c), 0)
                ):
                    continue
                stage = {
                    "type": "HOP",
                    "relationship_id": rel.id,
                    "sender": bank,
                    "receiver": rel.correspondent,
                    "currency": c,
                    "input_minor": amount,
                    "output_minor": amount - fee,
                    "fee_minor": fee,
                    "fee_rule": {
                        "fixed_minor": rel.fixed_fee_minor,
                        "bps": rel.variable_fee_bps,
                        "rounding": "FLOOR",
                    },
                    "charge_to": "BENEFICIARY",
                    "delay_ms": rel.delay_ms,
                }
                queue.append(
                    (rel.correspondent, c, amount - fee, path + [stage], next_visited, new_fees, fx_used)
                )
            if bank == "STERLING_UK" and c != target and not fx_used and f"{c}/{target}" in RATES:
                numerator, denominator = RATES[f"{c}/{target}"]
                output = convert(amount, numerator, denominator)
                quote = {
                    "id": f"q:{cmd.idempotency_key}:fx",
                    "payment_id": cmd.idempotency_key,
                    "source": c,
                    "target": target,
                    "direction": f"{target} per 1 {c}",
                    "numerator": numerator,
                    "denominator": denominator,
                    "principal_minor": amount,
                    "output_minor": output,
                    "fee_minor": 0,
                    "fee_currency": c,
                    "issued_ms": at_ms,
                    "expires_ms": at_ms + 30000,
                    "rounding": "FLOOR_TARGET_MINOR",
                    "provider": bank,
                }
                queue.append(
                    (
                        bank,
                        target,
                        output,
                        path
                        + [
                            {
                                "type": "FX",
                                "sender": bank,
                                "receiver": bank,
                                "currency": c,
                                "target_currency": target,
                                "input_minor": amount,
                                "output_minor": output,
                                "fee_minor": 0,
                                "delay_ms": 1,
                                "quote": quote,
                            }
                        ],
                        next_visited,
                        fees,
                        True,
                    )
                )
        raise DomainError("NO_ELIGIBLE_CORRESPONDENT_PATH")

    def create(self, run_id: str, cmd: ChainCommand, at_ms: int = 0):
        with self.store.transaction(run_id) as tx:
            old = tx.payment_record(cmd.idempotency_key)
            command = cmd.model_dump(mode="json")
            if old:
                if old["fingerprint"] != digest(command):
                    raise DomainError("IDEMPOTENCY_KEY_REUSED")
                return old["data"]
            if tx.run["config"].get("rail") != "CORRESPONDENT":
                raise DomainError("CORRESPONDENT_WORLD_REQUIRED")
            tx.time(at_ms)
            rels = [
                Relationship.model_validate(r["config"])
                for r in tx.rows("SELECT config FROM cb_relationships WHERE run_id=:r ORDER BY id")
            ]
            path, output, fees = self._plan(rels, cmd, at_ms)
            identity = cmd.idempotency_key
            reference = "SYN-CB-" + digest(command)[:24]
            instruction = {
                "payment_id": identity,
                "reference": reference,
                "path": path,
                "beneficiary_minor": output,
                "fees_minor": fees,
            }
            data = {
                "id": identity,
                "command": command,
                "rail": "CORRESPONDENT",
                "economic_status": "CREATED",
                "ack_status": "NOT_SENT",
                "reason": "INSTRUCTION_ACCEPTED",
                "submitted_ms": at_ms,
                "deadline_ms": at_ms + cmd.deadline_ms,
                "next_stage": 0,
                "last_posted_ms": at_ms,
                "reference": reference,
                "beneficiary_minor": output,
                "fees_minor": fees,
                "path": [cmd.sender_bank_id] + [p["receiver"] for p in path if p["type"] == "HOP"],
            }
            tx.create_payment(identity, digest(command), command, data)
            tx.payment_state(identity, data)
            tx.execute(
                "INSERT INTO cb_chains VALUES(:r,:p,:ref,CAST(:d AS jsonb))",
                p=identity,
                ref=reference,
                d=canonical(instruction),
            )
            tx.event("CB_CHAIN_CREATED", identity, instruction)
            for i, stage in enumerate(path):
                hop = {
                    **stage,
                    "id": f"{identity}:hop:{i}",
                    "payment_id": identity,
                    "hop_index": i,
                    "reference": reference,
                    "idempotency_key": f"{reference}-{i}",
                    "posting_status": "PENDING",
                    "delivery_status": "NOT_SENT",
                    "attempts": 0,
                    "journal_ids": [],
                }
                tx.execute(
                    "INSERT INTO cb_hops VALUES(:r,:p,:i,:id,:key,CAST(:d AS jsonb))",
                    p=identity,
                    i=i,
                    id=hop["id"],
                    key=hop["idempotency_key"],
                    d=canonical(hop),
                )
                tx.event("CB_HOP_CHANGED", identity, hop)
                if stage["type"] == "FX":
                    q = stage["quote"]
                    tx.execute(
                        "INSERT INTO quotes VALUES(:r,:id,:p,CAST(:d AS jsonb))",
                        id=q["id"],
                        p=identity,
                        d=canonical(q),
                    )
                    tx.event("QUOTE_LOCKED", identity, q)
            data.update(economic_status="QUEUED", reason="AWAITING_FIRST_HOP")
            tx.payment_state(identity, data)
            return data

    def _hop_state(self, tx, hop):
        tx.execute(
            "UPDATE cb_hops SET data=CAST(:d AS jsonb) WHERE run_id=:r AND id=:id",
            id=hop["id"],
            d=canonical(hop),
        )
        tx.event("CB_HOP_CHANGED", hop["payment_id"], hop)

    def _obligation(self, tx, payment, bank, account, currency, amount, at_ms, suffix):
        row = {
            "payment_id": payment,
            "id": f"{payment}:obligation:{suffix}",
            "debtor": bank,
            "account_id": account,
            "currency": currency,
            "amount": amount,
            "status": "OPEN",
            "opened_ms": at_ms,
            "closed_ms": None,
        }
        tx.execute(
            "INSERT INTO cb_obligations VALUES(:r,:payment_id,:id,:debtor,:account_id,:currency,:amount,:status,:opened_ms,:closed_ms)",
            **row,
        )
        tx.event("CB_OBLIGATION_CHANGED", payment, row)

    def _close(self, tx, row, at_ms, status="DISCHARGED"):
        row = {k: v for k, v in row.items() if k != "run_id"}
        row.update(status=status, closed_ms=at_ms)
        tx.execute(
            "UPDATE cb_obligations SET status=:s,closed_ms=:t WHERE run_id=:r AND id=:id",
            s=status,
            t=at_ms,
            id=row["id"],
        )
        tx.event("CB_OBLIGATION_CHANGED", row["payment_id"], row)

    def _blocked(self, tx, data, reason):
        status = "PENDING_EXPOSURE" if data["next_stage"] else "QUEUED"
        if data["economic_status"] != status or data["reason"] != reason:
            data.update(economic_status=status, reason=reason)
            tx.payment_state(data["id"], data)
        return data

    def advance(self, run_id: str, payment: str, at_ms: int, hop_index: int | None = None):
        """At most one hop or acknowledgment per call. Each call commits independently."""
        with self.store.transaction(run_id) as tx:
            data = tx.payment(payment)
            if data is None or data["rail"] != "CORRESPONDENT":
                raise DomainError("CHAIN_NOT_FOUND")
            tx.time(at_ms)
            chain = tx.one("SELECT instruction FROM cb_chains WHERE run_id=:r AND payment_id=:p", p=payment)[
                "instruction"
            ]
            record = tx.payment_record(payment)
            if data["command"] != record["command"] or digest(record["command"]) != record["fingerprint"]:
                raise DomainError("COMMAND_PROJECTION_MISMATCH")
            if hop_index is not None:
                requested = tx.one(
                    "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=:i",
                    p=payment,
                    i=hop_index,
                )
                if requested is None:
                    raise DomainError("HOP_NOT_FOUND")
                if requested["data"]["posting_status"] == "POSTED":
                    return data
                if hop_index != data["next_stage"]:
                    raise DomainError("OUT_OF_ORDER_HOP")
            active = [r["data"] for r in tx.active_faults()]

            def down(bank):
                return any(f["type"] == "PARTICIPANT_DOWN" and f["target"] == bank for f in active)

            # Re-read the durable hop after response loss, never repost it.
            if data["next_stage"]:
                prior = tx.one(
                    "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=:i",
                    p=payment,
                    i=data["next_stage"] - 1,
                )["data"]
                if prior["delivery_status"] != "ACKNOWLEDGED":
                    if down(prior["receiver"]) or any(
                        f["type"] in ("CRASH_AFTER_COMMIT", "MESSAGE_DROP")
                        and f["target"] == prior["receiver"]
                        for f in active
                    ):
                        return (
                            data
                            if data["economic_status"] == "SETTLED"
                            else self._blocked(tx, data, "POSTED_ACK_MISSING")
                        )
                    prior["delivery_status"] = "ACKNOWLEDGED"
                    self._hop_state(tx, prior)
                    if data["economic_status"] == "SETTLED":
                        data["ack_status"] = "ACKNOWLEDGED"
                        tx.payment_state(payment, data)
                    return data
            if data["economic_status"] in ("SETTLED", "EXPIRED", "ABORTED", "RETURNED", "REJECTED"):
                return data
            if at_ms >= data["deadline_ms"]:
                if data["next_stage"]:
                    return self._blocked(tx, data, "DEADLINE_PASSED_EXPOSURE_REQUIRES_MANUAL_RECOVERY")
                data.update(economic_status="EXPIRED", reason="DEADLINE_BEFORE_FIRST_POSTING")
                tx.payment_state(payment, data)
                return data
            i = data["next_stage"]
            hop = tx.one(
                "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=:i", p=payment, i=i
            )["data"]
            if (
                any(hop.get(k) != v for k, v in chain["path"][i].items())
                or hop["reference"] != data["reference"]
            ):
                raise DomainError("IMMUTABLE_CHAIN_PLAN_MISMATCH")
            if at_ms < data["last_posted_ms"] + hop["delay_ms"]:
                return data
            if down(hop["sender"]) or down(hop["receiver"]):
                return self._blocked(tx, data, "PARTICIPANT_DOWN")
            if any(f["type"] == "MESSAGE_DROP" and f["target"] == hop["receiver"] for f in active):
                if hop["delivery_status"] != "MESSAGE_LOST":
                    hop.update(delivery_status="MESSAGE_LOST", attempts=hop["attempts"] + 1)
                    self._hop_state(tx, hop)
                return self._blocked(tx, data, "ONWARD_MESSAGE_LOST")
            current = tx.one(
                "SELECT * FROM cb_obligations WHERE run_id=:r AND payment_id=:p AND status='OPEN'", p=payment
            )
            if hop["type"] == "FX":
                quote = tx.one("SELECT data FROM quotes WHERE run_id=:r AND id=:q", q=hop["quote"]["id"])[
                    "data"
                ]
                if quote != hop["quote"] or current is None or current["amount"] != quote["principal_minor"]:
                    raise DomainError("FX_CONTRACT_MISMATCH")
                if at_ms >= quote["expires_ms"]:
                    return self._blocked(tx, data, "FX_QUOTE_EXPIRED_AFTER_POSTING")
                next_relationship = chain["path"][i + 1]["relationship_id"]
                inventory = tx.rows(
                    "SELECT vostro_id FROM cb_relationships WHERE run_id=:r AND id=:id AND respondent=:b AND currency=:c",
                    id=next_relationship,
                    b=hop["sender"],
                    c=quote["target"],
                )
                if not any(
                    (lambda p: p["posted"] - p["reserved"] - p["buffer"] >= quote["output_minor"])(
                        tx.position(r["vostro_id"])
                    )
                    for r in inventory
                ):
                    return self._blocked(tx, data, "FX_PROVIDER_INVENTORY_SHORTAGE")
                bank = hop["sender"]
                target_account = f"TRANSIT:{payment}:{i}:{quote['target']}"
                tx.account(target_account, bank, quote["target"], "CLAIM", bank)
                source_id = f"{hop['id']}:fx-source"
                target_id = f"{hop['id']}:fx-target"
                tx.journal(
                    source_id,
                    payment,
                    quote["source"],
                    [
                        (current["account_id"], -quote["principal_minor"]),
                        (equity(bank, "FX", quote["source"]), quote["principal_minor"]),
                    ],
                    issuer=bank,
                )
                tx.journal(
                    target_id,
                    payment,
                    quote["target"],
                    [
                        (equity(bank, "FX", quote["target"]), -quote["output_minor"]),
                        (target_account, quote["output_minor"]),
                    ],
                    issuer=bank,
                )
                self._close(tx, current, at_ms)
                self._obligation(
                    tx, payment, bank, target_account, quote["target"], quote["output_minor"], at_ms, str(i)
                )
                journals = [source_id, target_id]
            else:
                rel = Relationship.model_validate(
                    tx.one(
                        "SELECT config FROM cb_relationships WHERE run_id=:r AND id=:id",
                        id=hop["relationship_id"],
                    )["config"]
                )
                day, clock = divmod(at_ms, DAY)
                if rel.status != "ACTIVE":
                    return self._blocked(tx, data, "RELATIONSHIP_DOWN")
                if day in rel.holidays or clock < rel.open_ms:
                    return self._blocked(tx, data, "OPERATING_WINDOW_CLOSED")
                if clock >= rel.cutoff_ms:
                    return self._blocked(tx, data, "CUTOFF_PASSED")
                amount = hop["input_minor"]
                if amount > min(rel.maximum_minor, rel.exposure_limit_minor):
                    return self._blocked(tx, data, "AMOUNT_OR_EXPOSURE_LIMIT")
                if i < len(chain["path"]) - 1:
                    existing_exposure = tx.one(
                        "SELECT COALESCE(sum(amount),0) n FROM cb_obligations WHERE run_id=:r AND debtor=:b AND currency=:c AND status='OPEN'",
                        b=rel.correspondent,
                        c=str(rel.currency),
                    )["n"]
                    if existing_exposure + hop["output_minor"] > rel.exposure_limit_minor:
                        return self._blocked(tx, data, "AGGREGATE_EXPOSURE_LIMIT")
                pos = tx.position(vostro(rel))
                if pos["posted"] - pos["reserved"] - pos["buffer"] < amount:
                    return self._blocked(tx, data, "INSUFFICIENT_CORRESPONDENT_LIQUIDITY")
                if i and (
                    current is None
                    or current["debtor"] != rel.respondent
                    or current["currency"] != str(rel.currency)
                    or current["amount"] != amount
                ):
                    raise DomainError("ONWARD_OBLIGATION_MISMATCH")
                hold = f"{hop['id']}:hold"
                tx.reserve(hold, payment, vostro(rel), amount, data["deadline_ms"])
                tx.resolve(payment, "CONSUMED", expected={hold: (vostro(rel), amount, data["deadline_ms"])})
                sender_id = f"{hop['id']}:respondent"
                receiver_id = f"{hop['id']}:correspondent"
                sender_debit = (
                    current["account_id"]
                    if current
                    else equity(rel.respondent, "PAYMENT_EXPENSE", rel.currency)
                )
                tx.journal(
                    sender_id,
                    payment,
                    rel.currency,
                    [(sender_debit, -amount), (nostro(rel), amount)],
                    issuer=rel.respondent,
                )
                last = i == len(chain["path"]) - 1
                target_account = (
                    f"BENEFICIARY:{payment}:{rel.currency}"
                    if last
                    else f"TRANSIT:{payment}:{i}:{rel.currency}"
                )
                tx.account(target_account, rel.correspondent, rel.currency, "CLAIM", rel.correspondent)
                lines = [(vostro(rel), -amount), (target_account, hop["output_minor"])]
                if hop["fee_minor"]:
                    lines.append((equity(rel.correspondent, "FEES", rel.currency), hop["fee_minor"]))
                tx.journal(receiver_id, payment, rel.currency, lines, issuer=rel.correspondent)
                if current:
                    self._close(tx, current, at_ms)
                if not last:
                    self._obligation(
                        tx,
                        payment,
                        rel.correspondent,
                        target_account,
                        str(rel.currency),
                        hop["output_minor"],
                        at_ms,
                        str(i),
                    )
                journals = [sender_id, receiver_id]
            hop.update(
                posting_status="POSTED",
                delivery_status="ACK_MISSING",
                attempts=hop["attempts"] + 1,
                journal_ids=journals,
                posted_ms=at_ms,
            )
            self._hop_state(tx, hop)
            data.update(
                next_stage=i + 1,
                last_posted_ms=at_ms,
                economic_status="IN_PROGRESS",
                reason="AWAITING_ONWARD_PROCESSING",
            )
            if data["next_stage"] == len(chain["path"]):
                data.update(
                    economic_status="SETTLED",
                    reason="BENEFICIARY_CREDITED",
                    settled_ms=at_ms,
                    ack_status="MISSING",
                    settlement_id=f"chain-settlement:{payment}",
                )
            tx.payment_state(payment, data)
            return data

    def return_first_hop(self, run_id: str, payment: str, at_ms: int):
        """Permitted compensation: only the first correspondent may return its unpaid claim; fees remain earned."""
        with self.store.transaction(run_id) as tx:
            data = tx.payment(payment)
            if data is None:
                raise DomainError("CHAIN_NOT_FOUND")
            if data["economic_status"] == "RETURNED":
                return data
            if data["economic_status"] == "SETTLED":
                raise DomainError("FINAL_BENEFICIARY_CREDIT")
            if data["next_stage"] != 1:
                raise DomainError("RETURN_REQUIRES_FIRST_HOP_ONLY")
            tx.time(at_ms)
            hop = tx.one(
                "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=0", p=payment
            )["data"]
            rel = Relationship.model_validate(
                tx.one(
                    "SELECT config FROM cb_relationships WHERE run_id=:r AND id=:id",
                    id=hop["relationship_id"],
                )["config"]
            )
            if any(
                f["data"]["type"] == "PARTICIPANT_DOWN" and f["data"]["target"] == rel.correspondent
                for f in tx.active_faults()
            ):
                raise DomainError("RETURN_HOLDER_DOWN")
            current = tx.one(
                "SELECT * FROM cb_obligations WHERE run_id=:r AND payment_id=:p AND status='OPEN'", p=payment
            )
            data.update(economic_status="RETURN_REQUESTED", reason="EXPLICIT_FIRST_HOP_RETURN")
            tx.payment_state(payment, data)
            amount = current["amount"]
            references = []
            for suffix, bank, lines in [
                ("holder", rel.correspondent, [(current["account_id"], -amount), (vostro(rel), amount)]),
                (
                    "origin",
                    rel.respondent,
                    [
                        (nostro(rel), -amount),
                        (equity(rel.respondent, "PAYMENT_EXPENSE", rel.currency), amount),
                    ],
                ),
            ]:
                identity = f"{payment}:compensation:{suffix}"
                tx.journal(identity, payment, rel.currency, lines, issuer=bank)
                references.append(identity)
            self._close(tx, current, at_ms, "RETURNED")
            tx.event(
                "CB_COMPENSATION_POSTED",
                payment,
                {
                    "original_journals": hop["journal_ids"],
                    "new_journals": references,
                    "amount_minor": amount,
                    "currency": str(rel.currency),
                    "fees_refunded": False,
                },
            )
            data.update(
                economic_status="RETURNED",
                reason="RETURNED_TO_ORIGINAL_NOSTRO_FEES_RETAINED",
                returned_ms=at_ms,
            )
            tx.payment_state(payment, data)
            return data
