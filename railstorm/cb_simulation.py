"""Deterministic correspondent ticks; each hop/tick has its own commit boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import text

from railstorm.domain import DomainError, Scheduler, canonical
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail, Relationship, default_relationships
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault


class CorrespondentScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    rail: Literal["CORRESPONDENT"] = "CORRESPONDENT"
    scenario_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    seed: StrictInt = 20261029
    payment_count: StrictInt = Field(default=1, ge=1, le=1000)
    interval_ms: StrictInt = Field(default=500, ge=1, le=100000)
    tick_ms: StrictInt = Field(default=50, ge=1, le=1000)
    recovery_duration_ms: StrictInt = Field(default=2000, ge=100, le=60000)
    principal_minor: StrictInt = Field(default=10000, gt=0, le=1000000000)
    destination_currency: Literal["USD", "SGD"] = "USD"
    deadline_ms: StrictInt = Field(default=30000, gt=0, le=60000)
    relationships: list[Relationship] = Field(default_factory=default_relationships, max_length=30)
    faults: list[Fault] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounds(self):
        if (
            self.payment_count * (self.recovery_duration_ms // self.tick_ms + 2) > 100000
            or self.interval_ms * self.payment_count + self.recovery_duration_ms > 3600000
        ):
            raise ValueError("CORRESPONDENT_QUEUE_LIMIT")
        if len({r.id for r in self.relationships}) != len(self.relationships) or len(
            {f.id for f in self.faults}
        ) != len(self.faults):
            raise ValueError("DUPLICATE_CONFIGURATION_ID")
        return self


def create_correspondent_run(store, run_id, scenario):
    CorrespondentRail(store).initialize(
        run_id, list(scenario.relationships), scenario.model_dump(mode="json")
    )
    queue = Scheduler()
    for fault in scenario.faults:
        queue.push(fault.at_ms, 0, "FAULT_ON", fault.model_dump())
        queue.push(fault.at_ms + fault.duration_ms, 0, "FAULT_OFF", fault.model_dump())
    for i in range(scenario.payment_count):
        at = i * scenario.interval_ms
        queue.push(at, 1, "CB_CREATE", {"index": i})
        for delta in range(scenario.tick_ms, scenario.recovery_duration_ms + 1, scenario.tick_ms):
            queue.push(at + delta, 2, "CB_TICK", {"index": i})
    events = [queue.pop() for _ in range(len(queue.queue))]
    with store.transaction(run_id) as tx:
        tx.execute("INSERT INTO runtime(run_id,queue) VALUES(:r,CAST(:q AS jsonb))", q=canonical(events))


def step_correspondent(store, run_id, count):
    if not 1 <= count <= 1000:
        raise DomainError("STEP_LIMIT")
    with store.engine.connect() as guard:
        if not guard.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:r,0))"), {"r": run_id}
        ).scalar():
            raise DomainError("RUN_BUSY")
        try:
            for _ in range(count):
                # One event/one hop operation commits independently, even when
                # a caller asks the HTTP worker for a batch of simulation ticks.
                with store.transaction(run_id) as tx:
                    runtime = tx.one(
                        "SELECT cursor,jsonb_array_length(queue) total,queue->cursor item FROM runtime WHERE run_id=:r"
                    )
                    if runtime["cursor"] >= runtime["total"]:
                        break
                    at, _, _, kind, payload = runtime["item"]
                    tx.time(at)
                    if kind.startswith("FAULT_"):
                        apply_fault(store, run_id, Fault.model_validate(payload), kind == "FAULT_ON", at)
                    else:
                        identity = f"p{payload['index']:06d}"
                        rail = CorrespondentRail(store)
                        if kind == "CB_CREATE":
                            config = tx.run["config"]
                            rail.create(
                                run_id,
                                ChainCommand(
                                    idempotency_key=identity,
                                    principal_minor=config["principal_minor"],
                                    destination_currency=config["destination_currency"],
                                    deadline_ms=config["deadline_ms"],
                                ),
                                at,
                            )
                        else:
                            result = rail.advance(run_id, identity, at)
                            for fault in tx.active_faults():
                                f = fault["data"]
                                if f["type"] == "DUPLICATE_DELIVERY" and result["next_stage"]:
                                    for _ in range(f["count"]):
                                        rail.advance(run_id, identity, at, result["next_stage"] - 1)
                    tx.execute("UPDATE runtime SET cursor=cursor+1 WHERE run_id=:r")
            with store.transaction(run_id) as tx:
                r = tx.one(
                    "SELECT cursor,paused,error,jsonb_array_length(queue) total_events FROM runtime WHERE run_id=:r"
                )
                done = r["cursor"] == r["total_events"]
                if done:
                    tx.execute("UPDATE runs SET status='COMPLETED' WHERE id=:r")
                return {**r, "done": done}
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(hashtextextended(:r,0))"), {"r": run_id})


class RecoveryView(BaseModel):
    forward_eligible: bool
    first_hop_return_eligible: bool
    reason: str


class HopView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    payment_id: str
    hop_index: int
    type: Literal["HOP", "FX"]
    sender: str
    receiver: str
    input_minor: int
    output_minor: int
    fee_minor: int
    posting_status: Literal["PENDING", "POSTED"]
    delivery_status: Literal["NOT_SENT", "MESSAGE_LOST", "ACK_MISSING", "ACKNOWLEDGED"]
    instruction_status: Literal["ACCEPTED"]
    settlement_status: Literal["POSTED_FINAL_WITHIN_MODEL", "AWAITING_POSTING"]
    journal_ids: list[str]
    event_ids: list[int]


class ChainView(BaseModel):
    schema_version: Literal[1] = 1
    rail: Literal["CORRESPONDENT"] = "CORRESPONDENT"
    payment: dict
    hops: list[HopView]
    obligations: list[dict]
    pending_exposure_minor: dict[str, int]
    beneficiary_credited: bool
    end_to_end_completed: bool
    recovery: RecoveryView
    events: list[dict]
    event_count: int
    events_truncated: bool
    next_event_cursor: int | None


def chain_inspection(tx, payment):
    data = tx.payment(payment)
    if not data or data.get("rail") != "CORRESPONDENT":
        raise DomainError("CHAIN_NOT_FOUND")
    hops = [
        r["data"]
        for r in tx.rows(
            "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p ORDER BY hop_index", p=payment
        )
    ]
    obligations = tx.rows(
        "SELECT *, COALESCE(closed_ms,:t)-opened_ms age_ms FROM cb_obligations WHERE run_id=:r AND payment_id=:p ORDER BY opened_ms,id",
        p=payment,
        t=tx.run["logical_time_ms"],
    )
    exposure: dict[str, int] = {}
    for ob in obligations:
        if ob["status"] == "OPEN":
            exposure[ob["currency"]] = exposure.get(ob["currency"], 0) + ob["amount"]
    events = tx.rows(
        "SELECT * FROM events WHERE run_id=:r AND aggregate_id=:p ORDER BY seq LIMIT 500", p=payment
    )
    event_count = tx.one("SELECT count(*) n FROM events WHERE run_id=:r AND aggregate_id=:p", p=payment)["n"]
    posted = sum(h["posting_status"] == "POSTED" for h in hops)
    terminal = data["economic_status"] in {"SETTLED", "RETURNED", "EXPIRED", "ABORTED", "REJECTED"}
    for h in hops:
        h["instruction_status"] = "ACCEPTED"
        h["settlement_status"] = (
            "POSTED_FINAL_WITHIN_MODEL" if h["posting_status"] == "POSTED" else "AWAITING_POSTING"
        )
        h["event_ids"] = [
            e["seq"] for e in events if e["event_type"] == "CB_HOP_CHANGED" and e["payload"]["id"] == h["id"]
        ]
    return ChainView.model_validate(
        {
            "payment": data,
            "hops": hops,
            "obligations": obligations,
            "pending_exposure_minor": exposure,
            "beneficiary_credited": data["economic_status"] == "SETTLED",
            "end_to_end_completed": data["economic_status"] == "SETTLED"
            and data["ack_status"] == "ACKNOWLEDGED",
            "recovery": {
                "forward_eligible": (not terminal and tx.run["logical_time_ms"] < data["deadline_ms"])
                or (data["economic_status"] == "SETTLED" and data["ack_status"] == "MISSING"),
                "first_hop_return_eligible": posted == 1 and not terminal,
                "reason": data["reason"],
            },
            "events": events,
            "event_count": event_count,
            "events_truncated": event_count > len(events),
            "next_event_cursor": events[-1]["seq"] if events and event_count > len(events) else None,
        }
    ).model_dump(mode="json")


def cb_metrics(tx):
    rows = tx.rows(
        "SELECT data->>'economic_status' status,count(*) n FROM payments WHERE run_id=:r GROUP BY status"
    )
    outcomes = {r["status"]: r["n"] for r in rows}
    exposures = tx.rows(
        "SELECT currency,sum(amount)::bigint amount_minor,count(*) obligations FROM cb_obligations WHERE run_id=:r AND status='OPEN' GROUP BY currency ORDER BY currency"
    )
    fees = tx.rows(
        "SELECT a.currency,sum(p.posted)::bigint amount_minor FROM accounts a JOIN positions p ON a.run_id=p.run_id AND a.id=p.account_id WHERE a.run_id=:r AND a.id LIKE 'GL:%:FEES:%' GROUP BY a.currency ORDER BY a.currency"
    )
    settled = tx.rows("SELECT data FROM payments WHERE run_id=:r AND data->>'economic_status'='SETTLED'")
    latency = sorted(p["data"]["settled_ms"] - p["data"]["submitted_ms"] for p in settled)
    usd = next((f["amount_minor"] for f in fees if f["currency"] == "USD"), 0)
    return {
        "submitted": sum(outcomes.values()),
        "settled": outcomes.get("SETTLED", 0),
        "statuses": outcomes,
        "before_deadline": sum(p["data"]["settled_ms"] <= p["data"]["deadline_ms"] for p in settled),
        "pending": sum(v for k, v in outcomes.items() if k in ("QUEUED", "IN_PROGRESS", "PENDING_EXPOSURE")),
        "missing_ack": tx.one(
            "SELECT count(*) n FROM payments WHERE run_id=:r AND data->>'ack_status'='MISSING'"
        )["n"],
        "fees_by_currency_minor": {f["currency"]: f["amount_minor"] for f in fees},
        "fees_usd_minor": usd if all(f["currency"] == "USD" or not f["amount_minor"] for f in fees) else None,
        "p95_ms": latency[max(0, (len(latency) * 95 + 99) // 100 - 1)] if latency else None,
        "exposure_by_currency": {
            e["currency"]: {"amount_minor": e["amount_minor"], "obligations": e["obligations"]}
            for e in exposures
        },
        "exposure": None,
        "exposure_scope": "Separate currencies and bilateral issuer domains; no mixed nominal sum",
        "candidate_rails": ["CORRESPONDENT"],
    }
