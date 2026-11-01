"""Versioned fixed-order routing. Selection is never financial commitment."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from railstorm.correspondent_adapter import CorrespondentAdapter
from railstorm.domain import BANKS, Currency, DomainError, PaymentCommand, canonical, convert, digest
from railstorm.ledger import RATES, Ledger
from railstorm.rails.correspondent import ChainCommand
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

Rail = Literal["ATOMIC_PVP", "CORRESPONDENT", "INSTANT_LINK"]


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    payment_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    source: str = "ATLAS_US"
    destination: str = "MERIDIAN_SG"
    source_currency: Currency = Currency.USD
    destination_currency: Currency = Currency.SGD
    amount_minor: StrictInt = Field(default=10000, gt=0, le=9000000000000000)
    payment_class: Literal["RETAIL", "WHOLESALE"] = "RETAIL"
    maximum_fee_minor: StrictInt = Field(default=25000, ge=0, le=9000000000000000)
    maximum_fees_by_currency: dict[Currency, StrictInt] | None = None
    maximum_latency_ms: StrictInt | None = Field(default=None, ge=0)
    created_ms: StrictInt = Field(default=0, ge=0)
    execute_from_ms: StrictInt = Field(default=0, ge=0)
    execute_until_ms: StrictInt = Field(default=30000, gt=0)
    permitted_rails: tuple[Rail, ...] = ("ATOMIC_PVP", "CORRESPONDENT", "INSTANT_LINK")
    finality: Literal["ATOMIC_REQUIRED", "INDEPENDENT_STAGES_ALLOWED"] = "INDEPENDENT_STAGES_ALLOWED"
    require_binding_quote: bool = True

    @model_validator(mode="after")
    def valid(self):
        if self.source not in BANKS or self.destination not in BANKS or self.source == self.destination:
            raise ValueError("INVALID_PARTIES")
        if self.maximum_fees_by_currency is not None and any(
            v < 0 or v > 9000000000000000 for v in self.maximum_fees_by_currency.values()
        ):
            raise ValueError("INVALID_FEE_BUDGET")
        if not self.permitted_rails or len(set(self.permitted_rails)) != len(self.permitted_rails):
            raise ValueError("INVALID_RAIL_SET")
        if not self.created_ms <= self.execute_from_ms < self.execute_until_ms:
            raise ValueError("INVALID_WINDOW")
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["fixed-order-v1", "LIQUIDITY_AWARE_V1", "LIQUIDITY_AWARE_V2"] = "fixed-order-v1"
    preference: tuple[Rail, ...] = ("INSTANT_LINK", "ATOMIC_PVP", "CORRESPONDENT")

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.preference)) != len(self.preference) or not self.preference:
            raise ValueError("INVALID_PREFERENCE")
        return self


class Network(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["isolated-worlds-v1"] = "isolated-worlds-v1"
    worlds: dict[Rail, str]
    unavailable: tuple[str, ...] = ()
    quote_available: bool = True
    quote_expires_ms: StrictInt = Field(default=30000, ge=0)


class Router:
    def __init__(self, store: Store):
        self.store = store

    def initialize(self, run):
        Ledger(self.store).initialize(
            run,
            {
                "rail": "ROUTING",
                "routing": True,
                "funding": {
                    "ATLAS_US:USD": 0,
                    "STERLING_UK:USD": 0,
                    "STERLING_UK:SGD": 0,
                    "MERIDIAN_SG:SGD": 0,
                },
            },
        )

    @staticmethod
    def intent_data(intent):
        data = intent.model_dump(mode="json")
        if data.get("maximum_fees_by_currency") is None:
            data.pop("maximum_fees_by_currency", None)
        return data

    def command(self, intent, rail):
        return PaymentCommand(
            idempotency_key=intent.idempotency_key,
            sender_bank_id=intent.source,
            receiver_bank_id=intent.destination,
            source_currency=intent.source_currency,
            destination_currency=intent.destination_currency,
            principal_minor=intent.amount_minor,
            max_fee_minor=min(
                intent.maximum_fee_minor,
                (intent.maximum_fees_by_currency or {}).get(intent.source_currency, intent.maximum_fee_minor),
            ),
            deadline_ms=min(3600000, intent.execute_until_ms - intent.created_ms),
            requested_rail=rail,
        )

    def enumerate(self, intent: Intent, network: Network, at_ms: int, policy: Policy):
        if type(at_ms) is not int or at_ms < 0:
            raise DomainError("INVALID_ROUTING_TIME")
        candidates = []
        for rail in ("ATOMIC_PVP", "CORRESPONDENT", "INSTANT_LINK"):
            c: dict[str, Any] = {
                "route_id": rail + "-v1",
                "rail": rail,
                "status": "FEASIBLE",
                "reasons": [],
                "fees_by_currency": {},
                "beneficiary_minor": None,
                "liquidity": [],
                "latency_ms": None,
                "finality": "ALL_OR_NOTHING" if rail == "ATOMIC_PVP" else "INDEPENDENT_FINAL_STAGES",
                "risks": [] if rail == "ATOMIC_PVP" else ["SOURCE_FINAL_DESTINATION_PENDING"],
                "execution_adapter": True,
                "failure_probability": None,
                "fx_spread_cost": None,
                "amount_range_minor": [1, 9000000000000000],
                "operating_schedule": "24H synthetic default",
                "source": intent.source,
                "destination": intent.destination,
                "currencies": [str(intent.source_currency), str(intent.destination_currency)],
            }

            def reject(status, code, **detail):
                c["reasons"].append({"code": code, **detail})
                if c["status"] == "FEASIBLE":
                    c["status"] = status

            if rail not in intent.permitted_rails:
                reject("STRUCTURALLY_INELIGIBLE", "RAIL_NOT_PERMITTED")
            if intent.finality == "ATOMIC_REQUIRED" and rail != "ATOMIC_PVP":
                reject("STRUCTURALLY_INELIGIBLE", "FINALITY_UNSATISFIED")
            if not intent.execute_from_ms <= at_ms < intent.execute_until_ms:
                reject("TEMPORARILY_UNAVAILABLE", "OUTSIDE_INTENT_WINDOW")
            if intent.source_currency != intent.destination_currency and (
                not network.quote_available or at_ms >= network.quote_expires_ms
            ):
                reject("QUOTE_UNAVAILABLE", "QUOTE_UNAVAILABLE_OR_EXPIRED")
            world = network.worlds.get(rail)
            if not world:
                reject("STRUCTURALLY_INELIGIBLE", "NO_MODELED_WORLD")
                candidates.append(c)
                continue
            with self.store.transaction(world) as tx:
                config = tx.run["config"]
                actual = config.get("rail", "ATOMIC_PVP")
                if actual != rail:
                    reject("STRUCTURALLY_INELIGIBLE", "WORLD_RAIL_MISMATCH")
                if at_ms < tx.run["logical_time_ms"]:
                    reject("TEMPORARILY_UNAVAILABLE", "WORLD_CLOCK_AHEAD")
                down = set(network.unavailable) | set(config.get("offline_banks", []))
                if any(x in down for x in (intent.source, intent.destination, "STERLING_UK")):
                    reject("TEMPORARILY_UNAVAILABLE", "PARTICIPANT_DOWN")
                if tx.active_faults():
                    reject("TEMPORARILY_UNAVAILABLE", "ACTIVE_FAULT_REQUIRES_RAIL_PREFLIGHT")
                source, target = str(intent.source_currency), str(intent.destination_currency)
                rate = [1, 1] if source == target else config.get("rates", RATES).get(source + "/" + target)
                if rail == "ATOMIC_PVP" and config.get("quote_schedule") and source != target:
                    quotes = [
                        q
                        for q in config["quote_schedule"]
                        if q["pair"] == source + "/" + target
                        and q["valid_from_ms"] <= at_ms < q["expires_ms"]
                    ]
                    quote = quotes[0] if len(quotes) == 1 else None
                    rate = [quote["numerator"], quote["denominator"]] if quote else None
                    c["quote_identity"] = (
                        {"id": quote["id"], "version": quote["version"], "expires_ms": quote["expires_ms"]}
                        if quote
                        else None
                    )
                from railstorm.market_quotes import current

                live_quote = (
                    current(tx, source, target) if rail == "ATOMIC_PVP" and source != target else None
                )
                if rail == "INSTANT_LINK" and current(tx, source, target):
                    reject("STRUCTURALLY_INELIGIBLE", "DYNAMIC_INSTANT_ROUTING_UNSUPPORTED")
                if live_quote:
                    rate = (
                        [live_quote["numerator"], live_quote["denominator"]]
                        if at_ms < live_quote["expires_ms"]
                        else None
                    )
                    c["quote_identity"] = {k: live_quote[k] for k in ("id", "version", "expires_ms")}
                if not rate:
                    reject("QUOTE_UNAVAILABLE", "UNMODELED_CURRENCY_PAIR")
                if rail == "CORRESPONDENT":
                    try:
                        details = CorrespondentAdapter(self.store).evaluate(tx, intent, at_ms, down)
                        for reason in details.pop("reasons"):
                            code = reason.pop("code")
                            reject(details["status"], code, **reason)
                        details.pop("status")
                        c.update(details)
                    except DomainError as exc:
                        reject("STRUCTURALLY_INELIGIBLE", exc.code)
                elif rate:
                    if rail == "INSTANT_LINK":
                        c["amount_range_minor"] = [1, 100000]
                        if (intent.source, intent.destination, source, target) != (
                            "ATLAS_US",
                            "MERIDIAN_SG",
                            "USD",
                            "SGD",
                        ):
                            reject("STRUCTURALLY_INELIGIBLE", "INSTANT_CORRIDOR_UNSUPPORTED")
                            candidates.append(c)
                            continue
                        rate = [133, 100]
                        fee = 25
                        latency = 30
                        if intent.payment_class != "RETAIL" or intent.amount_minor > 100000:
                            reject("STRUCTURALLY_INELIGIBLE", "INSTANT_RETAIL_CAP")
                        if not config.get("source_available", True):
                            reject("TEMPORARILY_UNAVAILABLE", "US_FAST_UNAVAILABLE")
                    else:
                        fee = (
                            live_quote["fee_minor"]
                            if live_quote
                            else config.get("fee_minor", 20000 if source != target else 0)
                        )
                        latency = 100
                    try:
                        out = convert(intent.amount_minor, *rate)
                    except DomainError as exc:
                        reject("STRUCTURALLY_INELIGIBLE", exc.code)
                        out = 0
                    if rail == "INSTANT_LINK" and out > 100000:
                        reject("STRUCTURALLY_INELIGIBLE", "DESTINATION_AMOUNT_CAP")
                    c.update(
                        fees_by_currency={source: fee}, beneficiary_minor=out, latency_ms=latency, rate=rate
                    )
                    required = {intent.source + ":" + source: intent.amount_minor + fee}
                    if source != target:
                        required["STERLING_UK:" + target] = out
                    for account, amount in required.items():
                        pos = tx.position(account)
                        available = pos["posted"] - pos["reserved"] - pos["buffer"]
                        c["liquidity"].append(
                            {
                                "account": account,
                                "currency": account.split(":")[1],
                                "required_minor": amount,
                                "available_minor": available,
                                "semantics": "shared claim reservation"
                                if rail == "ATOMIC_PVP"
                                else "prefunded domestic hold",
                            }
                        )
                        if available < amount:
                            reject(
                                "INSUFFICIENTLY_FUNDED",
                                "LIQUIDITY_SHORTAGE",
                                account=account,
                                required_minor=amount,
                                available_minor=available,
                                shortfall_minor=amount - available,
                            )
                    if fee > min(
                        intent.maximum_fee_minor,
                        (intent.maximum_fees_by_currency or {}).get(
                            intent.source_currency, intent.maximum_fee_minor
                        ),
                    ):
                        reject("STRUCTURALLY_INELIGIBLE", "FEE_LIMIT")
                if (
                    intent.maximum_latency_ms is not None
                    and c["latency_ms"] is not None
                    and c["latency_ms"] > intent.maximum_latency_ms
                ):
                    reject("STRUCTURALLY_INELIGIBLE", "LATENCY_LIMIT")
            if policy.version == "LIQUIDITY_AWARE_V2" and c["status"] == "FEASIBLE":
                from railstorm.optimizer_v2 import observe_exposure

                with self.store.transaction(world) as observation_tx:
                    c["exposure_observation"] = observe_exposure(observation_tx, c)
            candidates.append(c)
        from railstorm.optimizer import choose

        selected, scores = choose(candidates, policy)
        return {
            "intent": self.intent_data(intent),
            "network": network.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "at_ms": at_ms,
            "candidates": candidates,
            "selected": selected,
            "rationale": (
                "Minimize maximum required share of current account liquidity; then currency fee vector, modeled latency, fixed tie-break"
                if policy.version == "LIQUIDITY_AWARE_V1"
                else "Pareto fees/proceeds; exposure vector, nominal hold-time, summed scarcity, latency and deterministic tie-break"
                if policy.version == "LIQUIDITY_AWARE_V2"
                else "First feasible rail in versioned preference"
            )
            if selected
            else "NO_ROUTE",
            **({"policy_scores": scores} if policy.version != "fixed-order-v1" else {}),
            "comparability": "Independent rail worlds; no shared-bankroll performance comparison",
        }

    def evaluate(self, run, intent, network, at_ms=0, policy=None):
        policy = policy or Policy()
        result = self.enumerate(intent, network, at_ms, policy)
        record = {
            "id": intent.payment_id,
            "fingerprint": digest(self.intent_data(intent)),
            "evaluation": result,
            "bound_rail": None,
        }
        with self.store.transaction(run) as tx:
            if not tx.run["config"].get("routing"):
                raise DomainError("ROUTING_WORLD_REQUIRED")
            old = tx.one("SELECT data FROM routing_records WHERE run_id=:r AND id=:id", id=intent.payment_id)
            if old:
                if old["data"]["fingerprint"] != record["fingerprint"]:
                    raise DomainError("INTENT_ID_REUSED")
                return old["data"]
            reused = tx.one(
                "SELECT id FROM routing_records WHERE run_id=:r AND data->'evaluation'->'intent'->>'idempotency_key'=:key",
                key=intent.idempotency_key,
            )
            if reused:
                raise DomainError("IDEMPOTENCY_KEY_REUSED")
            tx.time(at_ms)
            self._save(tx, record)
        return record

    @staticmethod
    def _save(tx, record):
        tx.execute(
            "INSERT INTO routing_records VALUES(:r,:id,CAST(:d AS jsonb)) ON CONFLICT(run_id,id) DO UPDATE SET data=excluded.data",
            id=record["id"],
            d=canonical(record),
        )
        tx.event("ROUTING_RECORD", record["id"], record)

    def read(self, run, payment):
        with self.store.transaction(run) as tx:
            row = tx.one("SELECT data FROM routing_records WHERE run_id=:r AND id=:id", id=payment)
            if not row:
                raise DomainError("ROUTE_NOT_FOUND")
            return row["data"]

    def execute(self, run, payment, at_ms=0, prepare_only=False):
        from railstorm.audit import reconcile

        if reconcile(self.store, run)["financial_errors"]:
            raise DomainError("ROUTING_RECONCILIATION_REQUIRED")
        initial = self.read(run, payment)
        if not initial["bound_rail"]:
            old = initial["evaluation"]
            latest = self.enumerate(
                Intent.model_validate(old["intent"]),
                Network.model_validate(old["network"]),
                at_ms,
                Policy.model_validate(old["policy"]),
            )
            if not old["selected"] or not any(
                c["rail"] == old["selected"] and c["status"] == "FEASIBLE" for c in latest["candidates"]
            ):
                raise DomainError("EXECUTION_PREFLIGHT_CHANGED")
            prior = next(c for c in old["candidates"] if c["rail"] == old["selected"])
            current = next(c for c in latest["candidates"] if c["rail"] == old["selected"])
            if old["selected"] == "ATOMIC_PVP" and any(
                prior.get(key) != current.get(key)
                for key in ("rate", "quote_identity", "fees_by_currency", "beneficiary_minor")
            ):
                raise DomainError("EXECUTION_QUOTE_CHANGED")
        with self.store.transaction(run) as tx:
            row = tx.one("SELECT data FROM routing_records WHERE run_id=:r AND id=:id", id=payment)
            if not row:
                raise DomainError("ROUTE_NOT_FOUND")
            record = row["data"]
            e = record["evaluation"]
            intent = Intent.model_validate(e["intent"])
            selected = record["bound_rail"] or e["selected"]
            if not selected:
                raise DomainError("NO_ROUTE")
            if not record["bound_rail"]:
                record["bound_rail"] = selected
                command = (
                    CorrespondentAdapter.command(intent, intent.execute_until_ms - at_ms)
                    if selected == "CORRESPONDENT"
                    else self.command(intent, selected).model_copy(
                        update={"deadline_ms": min(3600000, intent.execute_until_ms - at_ms)}
                    )
                )
                record["execution_command"] = command.model_dump(mode="json")
                self._save(tx, record)
        # A durable binding precedes preparation in the rail world. Failure cannot cause fallback.
        network = Network.model_validate(e["network"])
        world = network.worlds[selected]
        cmd = (
            ChainCommand.model_validate(record["execution_command"])
            if selected == "CORRESPONDENT"
            else PaymentCommand.model_validate(record["execution_command"])
        )
        with self.store.transaction(world) as tx:
            existing = tx.payment(cmd.idempotency_key)
        if existing is None and at_ms >= intent.execute_until_ms:
            raise DomainError("EXECUTION_WINDOW_EXPIRED")
        if selected == "CORRESPONDENT":
            if reconcile(self.store, world)["financial_errors"]:
                raise DomainError("CORRESPONDENT_RECONCILIATION_REQUIRED")
            adapter = CorrespondentAdapter(self.store)
            state = (
                adapter.rail.create(world, ChainCommand.model_validate(record["execution_command"]), at_ms)
                if prepare_only
                else adapter.execute(world, cmd, at_ms)
            )
        elif selected == "ATOMIC_PVP":
            rail: Any = Ledger(self.store)
            # Quote validation and first financial preparation share the world lock.
            # An update between routing preflight and this transaction cannot silently reprice.
            with self.store.transaction(world) as execution_tx:
                if execution_tx.payment(cmd.idempotency_key) is None:
                    from railstorm.market_quotes import current

                    live = current(execution_tx, str(cmd.source_currency), str(cmd.destination_currency))
                    expected = next(c for c in e["candidates"] if c["rail"] == selected)
                    if live and (
                        expected.get("quote_identity")
                        != {k: live[k] for k in ("id", "version", "expires_ms")}
                        or expected.get("fees_by_currency") != {str(cmd.source_currency): live["fee_minor"]}
                    ):
                        raise DomainError("EXECUTION_QUOTE_CHANGED")
                state = rail.prepare(world, cmd, at_ms)
                if state["economic_status"] == "PREPARED" and not prepare_only:
                    state = rail.settle(world, cmd.idempotency_key, at_ms)
        else:
            rail = InstantLink(self.store)
            with self.store.transaction(world) as execution_tx:
                from railstorm.market_quotes import current

                if execution_tx.payment(cmd.idempotency_key) is None and current(execution_tx, "USD", "SGD"):
                    raise DomainError("DYNAMIC_INSTANT_ROUTING_UNSUPPORTED")
                state = rail.prepare(world, cmd, at_ms)
                if (
                    state["economic_status"] == "PREPARED"
                    and not state.get("source_final", False)
                    and not prepare_only
                ):
                    rail.deliver(world, cmd.idempotency_key, "SOURCE", max(at_ms, 10))
                    state = rail.inspect(world, cmd.idempotency_key)["payment"]
        return {"bound_rail": selected, "world": world, "payment": state, "fallback_permitted": False}

    def status(self, run, payment):
        record = self.read(run, payment)
        bound = record["bound_rail"]
        if not bound:
            return {"record": record, "payment": None}
        world = record["evaluation"]["network"]["worlds"][bound]
        with self.store.transaction(world) as tx:
            state = tx.payment(record["evaluation"]["intent"]["idempotency_key"])
        result = {"record": record, "world": world, "payment": state}
        if bound == "CORRESPONDENT" and state:
            result.update(CorrespondentAdapter(self.store).inspect(world, state["id"]))
        return result

    def reconcile(self, run, payment):
        from railstorm.audit import reconcile

        status = self.status(run, payment)
        return {
            "routing": reconcile(self.store, run),
            "settlement": reconcile(self.store, status["world"]) if status.get("world") else None,
        }

    def recover(self, run, payment, at_ms):
        status = self.status(run, payment)
        if status["record"]["bound_rail"] == "INSTANT_LINK":
            return InstantLink(self.store).recover(
                status["world"], status["record"]["evaluation"]["intent"]["idempotency_key"], at_ms
            )
        if status["record"]["bound_rail"] == "CORRESPONDENT":
            check = self.reconcile(run, payment)
            if check["routing"]["financial_errors"] or check["settlement"]["financial_errors"]:
                raise DomainError("CORRESPONDENT_RECONCILIATION_REQUIRED")
            return self.execute(run, payment, at_ms)
        if status["record"]["bound_rail"] == "ATOMIC_PVP":
            return self.execute(run, payment, at_ms)
        raise DomainError("NO_VERIFIED_BOUND_ADAPTER")
