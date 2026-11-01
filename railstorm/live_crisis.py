"""Bounded local operator sessions backed by native commands and durable events."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from railstorm.audit import reconcile, replay_check, snapshot
from railstorm.domain import DomainError, PaymentCommand
from railstorm.ledger import Ledger
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Router

SCENARIOS = {
    "atomic": "Atomic USD → SGD",
    "multicurrency": "Routed atomic USD → EUR",
    "correspondent": "Correspondent USD → USD",
    "instant": "Instant USD → SGD / live outage and recovery",
    "expired": "Atomic quote expiry",
    "shortage": "Atomic source liquidity shortage",
}


class LiveCrisis:
    def __init__(self, store):
        self.store = store

    def _save(self, session):
        with self.store.transaction(session["id"]) as tx:
            tx.event("CRISIS_SESSION", "operator-session", session)

    def initialize(self, scenario, seed):
        if scenario not in SCENARIOS:
            raise DomainError("UNSUPPORTED_LIVE_SCENARIO")
        run = "live-" + uuid.uuid4().hex
        session = {
            "id": run,
            "scenario": scenario,
            "seed": seed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "stage": "INITIALIZED",
            "payment_ids": ["payment"],
            "fault": "NONE",
            "recovery": "NOT_RUN",
            "audit": None,
            "replay": None,
            "steps": 0,
            "route_run": None,
        }
        if scenario == "instant":
            InstantLink(self.store).initialize(run)
        elif scenario == "correspondent":
            CorrespondentRail(self.store).initialize(run)
        else:
            config = {
                "fee_minor": 25,
                "quote_ttl_ms": 1 if scenario == "expired" else 30000,
                "rates": {"USD/SGD": [133, 100], "USD/EUR": [92, 100]},
                "funding": {
                    "STERLING_UK:EUR": 1000000,
                    "ATLAS_US:USD": 0 if scenario == "shortage" else 100000,
                },
            }
            Ledger(self.store).initialize(run, config)
            route = "live-route-" + uuid.uuid4().hex
            router = Router(self.store)
            router.initialize(route)
            from railstorm.domain import Currency

            intent = Intent(
                payment_id="payment",
                idempotency_key="payment",
                amount_minor=10000,
                destination="RHINE_EU" if scenario == "multicurrency" else "MERIDIAN_SG",
                destination_currency=Currency.EUR if scenario == "multicurrency" else Currency.SGD,
            )
            router.evaluate(route, intent, Network(worlds={"ATOMIC_PVP": run}))
            session["route_run"] = route
        self._save(session)
        return self.inspect(run)

    def inspect(self, run):
        with self.store.transaction(run) as tx:
            row = tx.one(
                "SELECT payload FROM events WHERE run_id=:r AND event_type='CRISIS_SESSION' ORDER BY seq DESC LIMIT 1"
            )
            if not row:
                raise DomainError("LIVE_SESSION_NOT_FOUND")
            session = row["payload"]
            state = snapshot(tx)
            logical = tx.run["logical_time_ms"]
            head = tx.run["next_seq"]
        route = Router(self.store).read(session["route_run"], "payment") if session["route_run"] else None
        return {
            "session": session,
            "financial": state,
            "logical_ms": logical,
            "event_head": head,
            "events": self.store.events(run, max(0, head - 100), 100),
            "route_decision": route,
            "supported_corridors": [
                "ATOMIC USD/SGD",
                "ATOMIC USD/EUR",
                "ATOMIC USD/INR",
                "ATOMIC EUR/SGD",
                "INSTANT USD/SGD",
                "CORRESPONDENT USD/SGD",
            ],
            "banks": ["ATLAS_US", "STERLING_UK", "RHINE_EU", "MERIDIAN_SG", "BHARAT_IN"],
            "currencies": ["USD", "SGD", "EUR", "INR"],
        }

    def action(self, run, action):
        # Dedicated connection lock serializes session orchestration across processes.
        # Financial commands retain their own run-row transactions and native guards.
        with self.store.engine.connect() as lock:
            lock.execute(text("SELECT pg_advisory_lock(hashtext(:r))"), {"r": run})
            try:
                return self._action(run, action)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(hashtext(:r))"), {"r": run})

    def _action(self, run, action):
        detail = self.inspect(run)
        session = detail["session"]
        if session["steps"] >= 120:
            raise DomainError("LIVE_SESSION_STEP_LIMIT")
        mode = session["scenario"]
        payment = detail["financial"]["payments"].get("payment")
        now = detail["logical_ms"]
        if action in ("OUTAGE", "LOSE_ACK", "RESTORE"):
            if mode != "instant" or not payment or payment.get("destination_final"):
                raise DomainError("ILLEGAL_LIVE_TRANSITION")
            if action == "RESTORE" and session["fault"] == "NONE":
                raise DomainError("NO_FAULT_TO_RESTORE")
            session["fault"] = {"OUTAGE": "DESTINATION_DOWN", "LOSE_ACK": "LOST_ACK", "RESTORE": "NONE"}[
                action
            ]
        elif action == "ADVANCE":
            if payment and payment["economic_status"] in ("SETTLED", "REJECTED", "EXPIRED", "ABORTED"):
                raise DomainError("LIVE_PAYMENT_TERMINAL")
            if not payment:
                if mode == "instant":
                    InstantLink(self.store).prepare(
                        run,
                        PaymentCommand(
                            idempotency_key="payment", principal_minor=10000, requested_rail="INSTANT_LINK"
                        ),
                        now,
                    )
                elif mode == "correspondent":
                    CorrespondentRail(self.store).create(run, ChainCommand(idempotency_key="payment"), now)
                else:
                    record = detail["route_decision"]
                    if not record["evaluation"]["selected"]:
                        # Native rejected intent remains visible; no alternate rail is invented.
                        Ledger(self.store).prepare(
                            run, PaymentCommand(idempotency_key="payment", principal_minor=10000), now
                        )
                    else:
                        Router(self.store).execute(session["route_run"], "payment", now, prepare_only=True)
            elif mode == "instant":
                stage = "DESTINATION" if payment["source_final"] else "SOURCE"
                at = max(
                    now + 1,
                    payment["destination_due_ms"] if stage == "DESTINATION" else payment["source_due_ms"],
                )
                InstantLink(self.store).deliver(
                    run,
                    "payment",
                    stage,
                    at,
                    available=session["fault"] != "DESTINATION_DOWN" if stage == "DESTINATION" else True,
                    lose_ack=session["fault"] == "LOST_ACK",
                )
            elif mode == "correspondent":
                CorrespondentRail(self.store).advance(run, "payment", now + 100)
            else:
                Router(self.store).execute(session["route_run"], "payment", now + 2)
        elif action in ("RECOVER", "RETRY"):
            if not payment:
                raise DomainError("PAYMENT_NOT_STARTED")
            if mode == "instant":
                InstantLink(self.store).recover(
                    run, "payment", max(now + 1, 40), available=session["fault"] != "DESTINATION_DOWN"
                )
            elif mode == "correspondent":
                CorrespondentRail(self.store).advance(run, "payment", now + 100)
            else:
                if payment["economic_status"] != "SETTLED":
                    raise DomainError("NO_ELIGIBLE_RECOVERY")
                Router(self.store).execute(session["route_run"], "payment", now + 1)
            session["recovery"] = "EXECUTED"
        elif action == "AUDIT":
            session["audit"] = {
                "native": reconcile(self.store, run),
                "independent": independent_audit(self.store, run),
                "at_event_head": detail["event_head"],
            }
        elif action == "REPLAY":
            session["replay"] = {**replay_check(self.store, run), "at_event_head": detail["event_head"]}
        else:
            raise DomainError("UNSUPPORTED_LIVE_ACTION")
        if action not in ("AUDIT", "REPLAY"):
            session["audit"] = None
            session["replay"] = None
        session["steps"] += 1
        refreshed = self.inspect(run)["financial"]["payments"].get("payment")
        session["stage"] = (
            refreshed.get("phase", refreshed["economic_status"]) if refreshed else "INITIALIZED"
        )
        self._save(session)
        return self.inspect(run)
