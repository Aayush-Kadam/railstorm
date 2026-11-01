"""Prefunded linked domestic settlement with independent final journal boundaries."""

from railstorm.domain import DomainError, PaymentCommand, Scheduler, canonical, convert, digest
from railstorm.ledger import INITIAL, RATES, Ledger
from railstorm.rails.domestic import default_systems
from railstorm.store import Store


class InstantLink:
    def __init__(self, store: Store):
        self.store = store

    def initialize(
        self, run: str, provider_sgd: int = 50000, sender_usd: int = 20000, source_available: bool = True
    ):
        funding = {a: 0 for a in INITIAL}
        funding.update({"ATLAS_US:USD": sender_usd, "STERLING_UK:SGD": provider_sgd})
        Ledger(self.store).initialize(
            run,
            {
                "rail": "INSTANT_LINK",
                "funding": funding,
                "instant_messages": True,
                "source_available": source_available,
            },
        )

    def prepare(self, run: str, cmd: PaymentCommand, at_ms: int = 0):
        with self.store.transaction(run) as tx:
            self._world(tx)
            old = tx.payment_record(cmd.idempotency_key)
            if old:
                if old["fingerprint"] != digest(cmd.model_dump(mode="json")):
                    raise DomainError("IDEMPOTENCY_KEY_REUSED")
                return old["data"]
            tx.time(at_ms)
            p = cmd.idempotency_key
            command = cmd.model_dump(mode="json")
            scheduler = Scheduler()
            scheduler.push(at_ms + 10, 1, "US_FAST_SETTLE", {"payment_id": p})
            scheduler.push(at_ms + 30, 2, "SG_FAST_SETTLE", {"payment_id": p})
            schedule = [scheduler.pop() for _ in range(len(scheduler.queue))]
            state = {
                "id": p,
                "rail": "INSTANT_LINK",
                "economic_status": "CREATED",
                "ack_status": "NOT_SENT",
                "command": command,
                "phase": "CREATED",
                "schedule": schedule,
                "source_due_ms": at_ms + 10,
                "deadline_ms": at_ms + cmd.deadline_ms,
                "obligation": None,
                "source_final": False,
                "destination_final": False,
            }
            tx.create_payment(p, digest(command), command, state)
            tx.payment_state(p, state)
            error = None
            us, sg = default_systems()
            output = 0
            try:
                if not tx.run["config"].get("source_available", True):
                    raise DomainError("US_FAST_UNAVAILABLE")
                if (
                    cmd.requested_rail != "INSTANT_LINK"
                    or cmd.source_currency != "USD"
                    or cmd.destination_currency != "SGD"
                ):
                    raise DomainError("UNSUPPORTED_INSTANT_CORRIDOR")
                if cmd.sender_bank_id != "ATLAS_US" or cmd.receiver_bank_id != "MERIDIAN_SG":
                    raise DomainError("UNSUPPORTED_INSTANT_PARTICIPANTS")
                us.validate_instruction(
                    cmd.sender_bank_id, "STERLING_UK", cmd.source_currency, cmd.principal_minor, at_ms
                )
                from railstorm.market_quotes import current

                market = current(tx, "USD", "SGD")
                if market and at_ms >= market["expires_ms"]:
                    raise DomainError("MARKET_QUOTE_EXPIRED")
                rate = [market["numerator"], market["denominator"]] if market else RATES["USD/SGD"]
                output = convert(cmd.principal_minor, *rate)
                sg.validate_instruction(
                    "STERLING_UK", cmd.receiver_bank_id, cmd.destination_currency, output, at_ms
                )
                if cmd.max_fee_minor < 25 or output < cmd.minimum_receive_minor:
                    raise DomainError("QUOTE_LIMIT")
                for a, n in [("ATLAS_US:USD", cmd.principal_minor + 25), ("STERLING_UK:SGD", output)]:
                    pos = tx.position(a)
                    if pos["posted"] - pos["reserved"] - pos["buffer"] < n:
                        raise DomainError("INSUFFICIENT_LIQUIDITY")
            except DomainError as exc:
                error = exc.code
            if error:
                state.update(economic_status="REJECTED", phase="REJECTED", reason=error)
            else:
                q = {
                    "id": f"q:{p}",
                    "payment_id": p,
                    "source": "USD",
                    "target": "SGD",
                    "numerator": rate[0],
                    "denominator": rate[1],
                    "principal_minor": cmd.principal_minor,
                    "output_minor": output,
                    "fee_minor": 25,
                    "expires_ms": min(at_ms + 30000, at_ms + cmd.deadline_ms),
                }
                if market:
                    q.update(
                        version=market["version"],
                        market_quote_id=market["id"],
                        expires_ms=min(q["expires_ms"], market["expires_ms"]),
                    )
                tx.execute(
                    "INSERT INTO quotes VALUES(:r,:id,:p,CAST(:d AS jsonb))", id=q["id"], p=p, d=canonical(q)
                )
                tx.event("QUOTE_LOCKED", p, q)
                tx.reserve(f"{p}:source-hold", p, "ATLAS_US:USD", cmd.principal_minor + 25, q["expires_ms"])
                tx.reserve(f"{p}:destination-hold", p, "STERLING_UK:SGD", output, q["expires_ms"])
                tx.decision(p, "PREPARED")
                state.update(economic_status="PREPARED", phase="FUNDS_RESERVED", quote=q)
            tx.payment_state(p, state)
            if not error:
                self._message(tx, p, "SOURCE", at_ms)
            return state

    @staticmethod
    def _world(tx):
        if tx.run["config"].get("rail") != "INSTANT_LINK":
            raise DomainError("WRONG_RAIL_WORLD")

    @staticmethod
    def _consume(tx, p, suffix, account, amount):
        row = tx.one("SELECT * FROM reservations WHERE run_id=:r AND id=:id", id=f"{p}:{suffix}")
        if not row or row["status"] != "ACTIVE" or row["account_id"] != account or row["amount"] != amount:
            raise DomainError("RESERVATION_MISMATCH")
        pos = tx.position(account)
        if pos["posted"] - pos["buffer"] < pos["reserved"]:
            raise DomainError("RESERVATION_UNFUNDED")
        tx.execute(
            "UPDATE positions SET reserved=reserved-:n WHERE run_id=:r AND account_id=:a", n=amount, a=account
        )
        tx.execute("UPDATE reservations SET status='CONSUMED' WHERE run_id=:r AND id=:id", id=row["id"])
        tx.event(
            "RESERVATION_CHANGED",
            p,
            {k: v for k, v in {**row, "status": "CONSUMED"}.items() if k != "run_id"},
        )

    def advance(
        self, run: str, payment: str, at_ms: int, destination_available: bool = True, drop_ack: bool = False
    ):
        with self.store.transaction(run) as tx:
            self._world(tx)
            tx.time(at_ms)
            record = tx.payment_record(payment)
            if not record:
                raise DomainError("PAYMENT_NOT_FOUND")
            state = record["data"]
            if state["economic_status"] in ("SETTLED", "REJECTED", "EXPIRED"):
                return state
            if digest(record["command"]) != record["fingerprint"] or state["command"] != record["command"]:
                raise DomainError("COMMAND_PROJECTION_MISMATCH")
            q = tx.one("SELECT data FROM quotes WHERE run_id=:r AND id=:id", id=f"q:{payment}")["data"]
            if state["quote"] != q:
                raise DomainError("QUOTE_PROJECTION_MISMATCH")
            if not state["source_final"]:
                if at_ms < state["source_due_ms"]:
                    return state
                if at_ms >= q["expires_ms"]:
                    tx.resolve(payment, "RELEASED")
                    tx.decision(payment, "ABORT")
                    state.update(economic_status="EXPIRED", phase="EXPIRED")
                else:
                    self._consume(tx, payment, "source-hold", "ATLAS_US:USD", q["principal_minor"] + 25)
                    tx.journal(
                        f"{payment}:us-final",
                        payment,
                        "USD",
                        [
                            ("ATLAS_US:USD", -q["principal_minor"] - 25),
                            ("STERLING_UK:USD", q["principal_minor"]),
                            ("OPERATOR:USD", 25),
                        ],
                        issuer="US_FAST",
                    )
                    tx.decision(payment, "COMMIT")
                    self._message(tx, payment, "DESTINATION", at_ms)
                    state.update(
                        source_final=True,
                        phase="SOURCE_SETTLED_DESTINATION_PENDING",
                        source_final_ms=at_ms,
                        destination_due_ms=at_ms + 20,
                        obligation={
                            "id": f"{payment}:bridge",
                            "currency": "SGD",
                            "amount_minor": q["output_minor"],
                            "owner": "STERLING_UK",
                            "beneficiary": "MERIDIAN_SG",
                            "created_ms": at_ms,
                            "reason": "DESTINATION_DELIVERY_PENDING",
                        },
                    )
                    tx.event(
                        "IL_SOURCE_FINAL", payment, {"system": "US_FAST", "journal": f"{payment}:us-final"}
                    )
            elif not destination_available:
                state.update(phase="SOURCE_SETTLED_DESTINATION_PENDING", reason="SG_FAST_UNAVAILABLE")
                tx.event("IL_DESTINATION_DELAYED", payment, {"system": "SG_FAST", "reason": "UNAVAILABLE"})
            elif at_ms >= state["destination_due_ms"]:
                self._consume(tx, payment, "destination-hold", "STERLING_UK:SGD", q["output_minor"])
                tx.journal(
                    f"{payment}:sg-final",
                    payment,
                    "SGD",
                    [("STERLING_UK:SGD", -q["output_minor"]), ("MERIDIAN_SG:SGD", q["output_minor"])],
                    issuer="SG_FAST",
                )
                state.update(
                    economic_status="SETTLED",
                    phase="COMPLETED",
                    destination_final=True,
                    destination_final_ms=at_ms,
                    obligation=None,
                    reason="BENEFICIARY_CREDITED",
                    ack_status="NOT_SENT" if drop_ack else "ACKNOWLEDGED",
                )
                tx.event(
                    "IL_DESTINATION_FINAL", payment, {"system": "SG_FAST", "journal": f"{payment}:sg-final"}
                )
            tx.payment_state(payment, state)
            return state

    @staticmethod
    def _save_message(tx, message):
        tx.execute(
            "INSERT INTO il_messages VALUES(:r,:id,CAST(:d AS jsonb)) ON CONFLICT(run_id,id) DO UPDATE SET data=excluded.data",
            id=message["id"],
            d=canonical(message),
        )
        tx.event("IL_MESSAGE_CHANGED", message["payment_id"], message)

    def _message(self, tx, payment, stage, at_ms):
        identity = f"{payment}:{stage.lower()}-message"
        message = {
            "id": identity,
            "payment_id": payment,
            "rail": "INSTANT_LINK",
            "type": stage,
            "source": "INSTANT_LINK_GATEWAY",
            "destination": "US_FAST" if stage == "SOURCE" else "SG_FAST",
            "idempotency_key": identity,
            "business_reference": payment,
            "created_ms": at_ms,
            "logical_ms": at_ms,
            "attempts": [],
            "delivery_status": "CREATED",
            "ack_status": "NOT_SENT",
            "last_error": None,
            "recovery_eligible": True,
        }
        self._save_message(tx, message)

    def messages(self, run, payment):
        with self.store.transaction(run) as tx:
            self._world(tx)
            return [
                r["data"]
                for r in tx.rows(
                    "SELECT data FROM il_messages WHERE run_id=:r AND data->>'payment_id'=:p ORDER BY id",
                    p=payment,
                )
            ]

    def deliver(self, run, payment, stage, at_ms, available=True, lose_ack=False):
        if stage not in {"SOURCE", "DESTINATION"}:
            raise DomainError("INVALID_MESSAGE_STAGE")
        with self.store.transaction(run) as tx:
            self._world(tx)
            tx.time(at_ms)
            row = tx.one(
                "SELECT data FROM il_messages WHERE run_id=:r AND id=:id",
                id=f"{payment}:{stage.lower()}-message",
            )
            if not row:
                raise DomainError("MESSAGE_NOT_READY")
            message = row["data"]
            if message["ack_status"] == "ACKNOWLEDGED":
                return message
            number = len(message["attempts"]) + 1
            message["attempts"].append({"id": f"{message['id']}:attempt:{number}", "at_ms": at_ms})
            message.update(logical_ms=at_ms, delivery_status="SENT", ack_status="PENDING")
            self._save_message(tx, message)
        # Network boundary: SENT commits before the participant processes the message.
        with self.store.transaction(run) as tx:
            state = tx.payment(payment)
        already_final = state["source_final" if stage == "SOURCE" else "destination_final"]
        if not available and stage == "SOURCE":
            state = None
        elif not already_final:
            state = self.advance(run, payment, at_ms, destination_available=available, drop_ack=lose_ack)
        with self.store.transaction(run) as tx:
            row = tx.one("SELECT data FROM il_messages WHERE run_id=:r AND id=:id", id=message["id"])
            message = row["data"]
            final = bool(state and state["source_final" if stage == "SOURCE" else "destination_final"])
            terminal = bool(state and state["economic_status"] in {"REJECTED", "EXPIRED"})
            message.update(
                delivery_status="TERMINAL_NO_SETTLEMENT" if terminal else "PROCESSED" if final else "DELAYED",
                ack_status="LOST"
                if final and lose_ack
                else "ACKNOWLEDGED"
                if final or terminal
                else "PENDING",
                last_error=None if final or terminal else "PARTICIPANT_UNAVAILABLE_OR_NOT_DUE",
                recovery_eligible=False if terminal else not final or lose_ack,
            )
            self._save_message(tx, message)
            return message

    def recover(self, run, payment, at_ms, available=True):
        from railstorm.audit import reconcile

        self.inspect(run, payment)
        with self.store.transaction(run) as tx:
            tx.time(at_ms)
        report = reconcile(self.store, run)
        if report["financial_errors"]:
            raise DomainError("RECOVERY_REQUIRES_RECONCILIATION")
        results = []
        for message in self.messages(run, payment):
            if message["recovery_eligible"]:
                results.append(self.deliver(run, payment, message["type"], at_ms, available))
        with self.store.transaction(run) as tx:
            tx.event(
                "IL_RECOVERY",
                payment,
                {"payment_id": payment, "at_ms": at_ms, "messages": [m["id"] for m in results]},
            )
        return {
            "action": "OPERATOR_RECOVERY",
            "messages": results,
            "verification": reconcile(self.store, run),
        }

    def recoverable(self, run):
        with self.store.transaction(run) as tx:
            self._world(tx)
            return tx.rows(
                "SELECT id,data FROM il_messages WHERE run_id=:r AND (data->>'recovery_eligible')::boolean ORDER BY id"
            )

    def inspect(self, run: str, payment: str):
        with self.store.transaction(run) as tx:
            self._world(tx)
            state = tx.payment(payment)
            if not state:
                raise DomainError("PAYMENT_NOT_FOUND")
            return {
                "payment": state,
                "logical_ms": tx.run["logical_time_ms"],
                "gateway": "INSTANT_LINK_GATEWAY",
                "messages": self.messages(run, payment),
                "systems": [s.model_dump(mode="json") for s in default_systems()],
                "positions": tx.rows("SELECT * FROM positions WHERE run_id=:r ORDER BY account_id"),
                "journals": tx.rows(
                    "SELECT * FROM journal_entries WHERE run_id=:r AND payment_id=:p ORDER BY id", p=payment
                ),
                "reservations": tx.rows(
                    "SELECT * FROM reservations WHERE run_id=:r AND payment_id=:p ORDER BY id", p=payment
                ),
                "obligation_age_ms": tx.run["logical_time_ms"] - state["obligation"]["created_ms"]
                if state["obligation"]
                else 0,
            }
