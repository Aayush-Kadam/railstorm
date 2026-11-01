"""Durable reservation and settlement commands; all funds are synthetic claims."""

from railstorm.domain import BANKS, DomainError, PaymentCommand, canonical, convert, digest
from railstorm.store import Store

INITIAL = {
    "ATLAS_US:USD": 1_000_000_000,
    "STERLING_UK:USD": 200_000_000,
    "STERLING_UK:SGD": 1_000_000_000,
    "MERIDIAN_SG:SGD": 100_000_000,
}
RATES = {"USD/SGD": [133, 100], "EUR/SGD": [145, 100], "USD/INR": [83, 1]}


class Ledger:
    def __init__(self, store: Store):
        self.store = store

    def initialize(self, run_id: str, config: dict | None = None, after_funding=None):
        config = config or {}
        schedule = config.get("quote_schedule", [])
        seen = set()
        for quote in schedule:
            required = ("id", "version", "pair", "numerator", "denominator", "valid_from_ms", "expires_ms")
            if not isinstance(quote, dict) or any(key not in quote for key in required):
                raise DomainError("INVALID_QUOTE_SCHEDULE")
            if (
                not isinstance(quote["id"], str)
                or not quote["id"]
                or quote["id"] in seen
                or any(type(quote[k]) is not int for k in required if k not in ("id", "pair"))
                or min(quote["version"], quote["numerator"], quote["denominator"]) <= 0
                or not 0 <= quote["valid_from_ms"] < quote["expires_ms"]
                or quote["pair"]
                not in {
                    f"{a}/{b}"
                    for a in ("USD", "EUR", "SGD", "INR")
                    for b in ("USD", "EUR", "SGD", "INR")
                    if a != b
                }
            ):
                raise DomainError("INVALID_QUOTE_SCHEDULE")
            seen.add(quote["id"])
        for i, left in enumerate(schedule):
            for right in schedule[i + 1 :]:
                if left["pair"] == right["pair"] and max(left["valid_from_ms"], right["valid_from_ms"]) < min(
                    left["expires_ms"], right["expires_ms"]
                ):
                    raise DomainError("AMBIGUOUS_QUOTE_SCHEDULE")
        funding = {**INITIAL, **config.get("funding", {})}
        with self.store.transaction(run_id, initialize_config=config) as tx:
            for bank in [*BANKS, "OPERATOR", "OPENING"]:
                for currency in ("USD", "EUR", "SGD", "INR"):
                    tx.account(
                        f"{bank}:{currency}",
                        bank,
                        currency,
                        "OPENING_OFFSET" if bank == "OPENING" else "CLAIM",
                        issuer=(
                            {"USD": "US_FAST", "SGD": "SG_FAST"}.get(currency, "SHARED")
                            if config.get("rail") == "INSTANT_LINK"
                            else "SHARED"
                        ),
                    )
            for account, amount in sorted(funding.items()):
                if type(amount) is not int or amount < 0:
                    raise DomainError("INVALID_FUNDING")
                if amount:
                    currency = account.split(":")[1]
                    tx.journal(
                        f"opening:{account}",
                        None,
                        currency,
                        [(f"OPENING:{currency}", -amount), (account, amount)],
                        issuer=(
                            {"USD": "US_FAST", "SGD": "SG_FAST"}.get(currency, "SHARED")
                            if config.get("rail") == "INSTANT_LINK"
                            else "SHARED"
                        ),
                    )
            tx.event("WORLD_INITIALIZED", "world", {"banks": BANKS, "config": config, "funds": "SYNTHETIC"})
            if after_funding is not None:
                after_funding(tx)

    def prepare(self, run_id: str, cmd: PaymentCommand, at_ms: int = 0):
        payment = cmd.idempotency_key
        command = cmd.model_dump(mode="json")
        fingerprint = digest(command)
        with self.store.transaction(run_id) as tx:
            existing = tx.payment_record(payment)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise DomainError("IDEMPOTENCY_KEY_REUSED")
                return existing["data"]
            if tx.run["config"].get("rail") in {"CORRESPONDENT", "INSTANT_LINK"}:
                raise DomainError("USE_CORRESPONDENT_CHAIN_COMMAND")
            tx.time(at_ms)
            state = {
                "id": payment,
                "economic_status": "CREATED",
                "ack_status": "NOT_SENT",
                "reason": "ACCEPTED",
                "rail": cmd.requested_rail,
                "submitted_ms": at_ms,
                "deadline_ms": at_ms + cmd.deadline_ms,
                "command": command,
            }
            tx.create_payment(payment, fingerprint, command, state)
            tx.payment_state(payment, state)
            config = tx.run["config"]
            active_faults = tx.active_faults()
            offline = set(config.get("offline_banks", []))
            for fault in active_faults:
                if fault["data"]["type"] == "PARTICIPANT_DOWN":
                    offline.add(fault["data"]["target"])
            error = None
            if cmd.requested_rail not in ("ATOMIC_PVP", "AUTO"):
                error = "RAIL_NOT_IMPLEMENTED"
            if any(b in offline for b in (cmd.sender_bank_id, cmd.receiver_bank_id, "STERLING_UK")):
                error = "PARTICIPANT_DOWN"
            source, target = str(cmd.source_currency), str(cmd.destination_currency)
            fee = config.get("fee_minor", 20_000 if source != target else 0)
            if type(fee) is not int or fee < 0:
                raise DomainError("INVALID_FEE")
            rate = [1, 1] if source == target else config.get("rates", RATES).get(f"{source}/{target}")
            scheduled_quote = None
            schedule = config.get("quote_schedule", [])
            if schedule and source != target:
                eligible = [
                    q
                    for q in schedule
                    if q["pair"] == f"{source}/{target}" and q["valid_from_ms"] <= at_ms < q["expires_ms"]
                ]
                if len(eligible) > 1:
                    raise DomainError("AMBIGUOUS_QUOTE_SCHEDULE")
                scheduled_quote = eligible[0] if eligible else None
                rate = (
                    [scheduled_quote["numerator"], scheduled_quote["denominator"]]
                    if scheduled_quote
                    else None
                )
            for fault in active_faults:
                f = fault["data"]
                if (
                    not schedule
                    and f["type"] == "FX_RATE_SHOCK"
                    and source != target
                    and target == f["currency"]
                ):
                    rate = [f["numerator"], f["denominator"]]
            from railstorm.market_quotes import current

            live_quote = current(tx, source, target) if source != target else None
            if live_quote:
                scheduled_quote = live_quote
                fee = live_quote["fee_minor"]
                rate = (
                    [live_quote["numerator"], live_quote["denominator"]]
                    if at_ms < live_quote["expires_ms"]
                    else None
                )
            if rate is None:
                error = "UNSUPPORTED_CORRIDOR"
            output = convert(cmd.principal_minor, *rate) if rate else 0
            if fee > cmd.max_fee_minor:
                error = "FEE_LIMIT"
            if output < cmd.minimum_receive_minor:
                error = "MINIMUM_OUTPUT"
            payer = f"{cmd.sender_bank_id}:{source}"
            provider = f"STERLING_UK:{target}"
            required = {payer: cmd.principal_minor + fee}
            if source != target:
                required[provider] = required.get(provider, 0) + output
            if not error:
                for account, amount in required.items():
                    position = tx.position(account)
                    if position["posted"] - position["reserved"] - position["buffer"] < amount:
                        error = "INSUFFICIENT_LIQUIDITY"
            if error:
                state.update(economic_status="REJECTED", reason=error)
                tx.payment_state(payment, state)
                return state
            ttl = config.get("quote_ttl_ms", 30_000)
            quote = {
                "id": f"q:{payment}",
                "payment_id": payment,
                "source": source,
                "target": target,
                "direction": f"{target} per 1 {source}",
                "numerator": rate[0],
                "denominator": rate[1],
                "principal_minor": cmd.principal_minor,
                "output_minor": output,
                "fee_minor": fee,
                "fee_currency": source,
                "issued_ms": at_ms,
                "expires_ms": at_ms + ttl,
                "rounding": "FLOOR_TARGET_MINOR",
                "provider": "STERLING_UK",
            }
            if scheduled_quote is not None:
                quote.update(
                    schedule_quote_id=scheduled_quote["id"],
                    version=scheduled_quote["version"],
                    valid_from_ms=scheduled_quote["valid_from_ms"],
                    provenance="DETERMINISTIC_SYNTHETIC_SCHEDULE",
                    expires_ms=min(quote["expires_ms"], scheduled_quote["expires_ms"]),
                )
            tx.execute(
                "INSERT INTO quotes VALUES(:r,:id,:p,CAST(:d AS jsonb))",
                id=quote["id"],
                p=payment,
                d=canonical(quote),
            )
            tx.event("QUOTE_LOCKED", payment, quote)
            for index, (account, amount) in enumerate(sorted(required.items())):
                tx.reserve(
                    f"{payment}:r{index}",
                    payment,
                    account,
                    amount,
                    min(quote["expires_ms"], state["deadline_ms"]),
                )
            tx.decision(payment, "PREPARED")
            state.update(economic_status="PREPARED", reason="FUNDS_RESERVED", quote=quote, rail="ATOMIC_PVP")
            tx.payment_state(payment, state)
            return state

    def settle(self, run_id: str, payment: str, at_ms: int = 0):
        with self.store.transaction(run_id) as tx:
            if tx.run["config"].get("rail") == "INSTANT_LINK":
                raise DomainError("USE_INSTANT_LINK_COMMAND")
            state = tx.payment(payment)
            if state is None:
                raise DomainError("PAYMENT_NOT_FOUND")
            if state["economic_status"] in ("SETTLED", "REJECTED", "EXPIRED", "ABORTED"):
                return state
            if state["economic_status"] != "PREPARED":
                raise DomainError("ILLEGAL_TRANSITION")
            tx.time(at_ms)
            record = tx.payment_record(payment)
            cmd = record["command"]
            if state.get("command") != cmd or digest(cmd) != record["fingerprint"]:
                raise DomainError("COMMAND_PROJECTION_MISMATCH")
            immutable = tx.one("SELECT data FROM quotes WHERE run_id=:r AND id=:q", q=f"q:{payment}")
            if immutable is None or state.get("quote") != immutable["data"]:
                raise DomainError("QUOTE_PROJECTION_MISMATCH")
            quote = immutable["data"]
            if at_ms >= min(quote["expires_ms"], state["deadline_ms"]):
                tx.resolve(payment, "RELEASED")
                tx.decision(payment, "ABORT")
                state.update(economic_status="EXPIRED", reason="QUOTE_OR_DEADLINE_EXPIRED")
                tx.payment_state(payment, state)
                return state
            active = tx.active_faults()
            if any(
                f["data"]["type"] == "PARTICIPANT_DOWN"
                and f["data"]["target"] in (cmd["sender_bank_id"], cmd["receiver_bank_id"], "STERLING_UK")
                for f in active
            ):
                state["reason"] = "PARTICIPANT_DOWN_PREPARED"
                tx.payment_state(payment, state)
                return state
            # All reservations were durable; consume before debiting under the same run lock/transaction.
            requirements = {
                f"{cmd['sender_bank_id']}:{quote['source']}": quote["principal_minor"] + quote["fee_minor"]
            }
            if quote["source"] != quote["target"]:
                account = f"STERLING_UK:{quote['target']}"
                requirements[account] = requirements.get(account, 0) + quote["output_minor"]
            expiry = min(quote["expires_ms"], state["deadline_ms"])
            expected = {
                f"{payment}:r{i}": (account, amount, expiry)
                for i, (account, amount) in enumerate(sorted(requirements.items()))
            }
            tx.resolve(payment, "CONSUMED", expected=expected)
            source, target = quote["source"], quote["target"]
            payer = f"{cmd['sender_bank_id']}:{source}"
            receiver = f"{cmd['receiver_bank_id']}:{target}"
            principal, output, fee = quote["principal_minor"], quote["output_minor"], quote["fee_minor"]
            source_lines = [(payer, -(principal + fee))]
            source_lines.append((receiver if source == target else f"STERLING_UK:{source}", principal))
            if fee:
                source_lines.append((f"OPERATOR:{source}", fee))
            tx.decision(payment, "COMMIT")
            tx.journal(f"{payment}:source", payment, source, source_lines)
            if source != target:
                tx.journal(
                    f"{payment}:target",
                    payment,
                    target,
                    [(f"STERLING_UK:{target}", -output), (receiver, output)],
                )
            state.update(
                economic_status="SETTLED",
                reason="ATOMIC_COMMIT",
                settled_ms=at_ms,
                settlement_id=f"settlement:{payment}",
                ack_status="MISSING",
            )
            tx.payment_state(payment, state)
            return state

    def acknowledge(self, run_id: str, payment: str):
        with self.store.transaction(run_id) as tx:
            state = tx.payment(payment)
            if state is None or state["economic_status"] != "SETTLED":
                raise DomainError("NOT_SETTLED")
            if state["ack_status"] != "ACKNOWLEDGED":
                state["ack_status"] = "ACKNOWLEDGED"
                tx.payment_state(payment, state)
            return state

    def abort(self, run_id: str, payment: str):
        """Abort only a prepared shared-book contract; final commits are immutable."""
        with self.store.transaction(run_id) as tx:
            state = tx.payment(payment)
            if state is None:
                raise DomainError("PAYMENT_NOT_FOUND")
            if state["economic_status"] == "ABORTED":
                return state
            if state["economic_status"] == "SETTLED":
                raise DomainError("COMMIT_CANNOT_ABORT")
            if state["economic_status"] != "PREPARED":
                raise DomainError("ILLEGAL_TRANSITION")
            tx.resolve(payment, "RELEASED")
            tx.decision(payment, "ABORT")
            state.update(economic_status="ABORTED", reason="OPERATOR_ABORT")
            tx.payment_state(payment, state)
            return state

    def submit(self, run_id: str, cmd: PaymentCommand, at_ms: int = 0, drop_ack: bool = False):
        state = self.prepare(run_id, cmd, at_ms)
        if state["economic_status"] == "PREPARED":
            state = self.settle(run_id, cmd.idempotency_key, at_ms)
        if state["economic_status"] == "SETTLED" and not drop_ack:
            state = self.acknowledge(run_id, cmd.idempotency_key)
        return state
