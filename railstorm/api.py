"""Local-only command center API. Authoritative financial writes stay in Ledger."""

import asyncio
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from railstorm import __version__
from railstorm.audit import reconcile, replay_check
from railstorm.cb_simulation import (
    ChainView,
    CorrespondentScenario,
    chain_inspection,
    create_correspondent_run,
)
from railstorm.domain import BANKS, DomainError, PaymentCommand, canonical, digest
from railstorm.ledger import Ledger
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail
from railstorm.recovery import recover_reservations
from railstorm.scenarios import Fault, Scenario
from railstorm.simulation import create_run, metrics, set_paused, step
from railstorm.store import Store


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: StrictInt = Field(default=1, ge=1, le=1000)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: PaymentCommand
    drop_ack: bool = False


class VirtualTimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at_ms: StrictInt = Field(ge=0, le=86400000)
    hop_index: StrictInt | None = Field(default=None, ge=0, le=6)


def create_app(store: Store | None = None):
    app = FastAPI(title="RAILSTORM · synthetic settlement laboratory", version=__version__)
    db = store or Store()
    from railstorm.live_crisis_api import register

    register(app, db)
    workers: dict[str, threading.Thread] = {}
    workers_lock = threading.Lock()

    @app.exception_handler(DomainError)
    async def domain_error(_, exc):
        return JSONResponse(
            status_code=404 if exc.code.endswith("NOT_FOUND") else 409,
            content={"schema_version": 1, "error": {"code": exc.code}},
        )

    @app.exception_handler(IntegrityError)
    async def conflict(_, exc):
        return JSONResponse(
            status_code=409, content={"schema_version": 1, "error": {"code": "DATABASE_CONSTRAINT"}}
        )

    def background(run_id):
        try:
            while True:
                with db.transaction(run_id) as tx:
                    runtime = tx.one("SELECT paused FROM runtime WHERE run_id=:r")
                    if runtime is None or runtime["paused"]:
                        return
                if step(db, run_id, 50)["done"]:
                    return
        except Exception:
            with db.transaction(run_id) as tx:
                tx.execute("UPDATE runtime SET paused=true,error='EXECUTION_FAILED' WHERE run_id=:r")
                tx.execute("UPDATE runs SET status='ERROR' WHERE id=:r")

    @app.get("/api/v1/health")
    def health():
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"schema_version": 1, "status": "OK", "engine": __version__, "funds": "SYNTHETIC"}

    @app.get("/api/v1/scenarios")
    def scenarios():
        with db.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text("SELECT * FROM scenarios ORDER BY id")).mappings()]

    @app.post("/api/v1/scenarios", status_code=201)
    def add_scenario(scenario: Scenario):
        config = scenario.model_dump()
        with db.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO scenarios VALUES(:id,CAST(:c AS jsonb),:f)"),
                {"id": scenario.scenario_id, "c": canonical(config), "f": digest(config)},
            )
        return {"id": scenario.scenario_id, "fingerprint": digest(config)}

    @app.post("/api/v1/correspondent/scenarios", status_code=201)
    def add_correspondent_scenario(scenario: CorrespondentScenario):
        config = scenario.model_dump(mode="json")
        with db.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO scenarios VALUES(:id,CAST(:c AS jsonb),:f)"),
                {"id": scenario.scenario_id, "c": canonical(config), "f": digest(config)},
            )
        return {"id": scenario.scenario_id, "fingerprint": digest(config), "rail": "CORRESPONDENT"}

    @app.get("/api/v1/runs")
    def runs():
        with db.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT id,status,logical_time_ms,fingerprint FROM runs ORDER BY created_at DESC,id LIMIT 100"
                    )
                ).mappings()
            ]

    @app.post("/api/v1/runs", status_code=201)
    def add_run(request: RunRequest):
        with db.engine.connect() as conn:
            row = conn.execute(
                text("SELECT config FROM scenarios WHERE id=:s"), {"s": request.scenario_id}
            ).first()
        if row is None:
            raise DomainError("SCENARIO_NOT_FOUND")
        run_id = "run-" + uuid.uuid4().hex[:16]
        if row[0].get("rail") == "CORRESPONDENT":
            create_correspondent_run(db, run_id, CorrespondentScenario.model_validate(row[0]))
        else:
            create_run(db, run_id, Scenario.model_validate(row[0]))
        return {"id": run_id, "status": "READY"}

    @app.get("/api/v1/runs/{run_id}")
    def run_detail(run_id: str):
        with db.transaction(run_id) as tx:
            runtime = tx.one(
                "SELECT cursor,paused,error,jsonb_array_length(queue) total_events FROM runtime WHERE run_id=:r"
            )
            return {**tx.run, "runtime": runtime, "schema_version": 1}

    @app.post("/api/v1/runs/{run_id}/step")
    def advance(run_id: str, request: StepRequest):
        return step(db, run_id, request.count)

    @app.post("/api/v1/runs/{run_id}/pause")
    def pause(run_id: str):
        set_paused(db, run_id, True)
        return {"status": "PAUSE_REQUESTED", "boundary": "after current batch, at most 50 virtual events"}

    @app.post("/api/v1/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str):
        with workers_lock:
            if run_id in workers and workers[run_id].is_alive():
                raise DomainError("RUN_BUSY")
            set_paused(db, run_id, False)
            worker = threading.Thread(target=background, args=(run_id,), daemon=True)
            workers[run_id] = worker
            worker.start()
        return {"status": "RUNNING", "id": run_id}

    @app.post("/api/v1/runs/{run_id}/payments")
    def submit(run_id: str, request: SubmitRequest):
        with db.transaction(run_id) as tx:
            at = tx.run["logical_time_ms"]
        return Ledger(db).submit(run_id, request.command, at, request.drop_ack)

    @app.post("/api/v1/runs/{run_id}/payments/{payment_id}/acknowledge")
    def acknowledge(run_id: str, payment_id: str):
        return Ledger(db).acknowledge(run_id, payment_id)

    @app.post("/api/v1/runs/{run_id}/recover-reservations")
    def recovery(run_id: str, request: VirtualTimeRequest):
        return recover_reservations(db, run_id, request.at_ms)

    @app.get("/api/v1/runs/{run_id}/chains/{payment_id}", response_model=ChainView)
    def inspect_chain(run_id: str, payment_id: str):
        with db.transaction(run_id) as tx:
            return chain_inspection(tx, payment_id)

    @app.post("/api/v1/runs/{run_id}/chains", status_code=201)
    def create_chain(run_id: str, command: ChainCommand):
        with db.transaction(run_id) as tx:
            at = tx.run["logical_time_ms"]
        return CorrespondentRail(db).create(run_id, command, at)

    @app.post("/api/v1/runs/{run_id}/chains/{payment_id}/advance")
    def advance_chain(run_id: str, payment_id: str, request: VirtualTimeRequest):
        return CorrespondentRail(db).advance(run_id, payment_id, request.at_ms, request.hop_index)

    @app.post("/api/v1/runs/{run_id}/chains/{payment_id}/return")
    def return_chain(run_id: str, payment_id: str, request: VirtualTimeRequest):
        return CorrespondentRail(db).return_first_hop(run_id, payment_id, request.at_ms)

    @app.get("/api/v1/runs/{run_id}/payments")
    def payments(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        with db.transaction(run_id) as tx:
            return tx.rows(
                "SELECT id,data FROM payments WHERE run_id=:r ORDER BY id LIMIT :n OFFSET :o",
                n=limit,
                o=offset,
            )

    @app.get("/api/v1/runs/{run_id}/payments/{payment_id}")
    def payment_detail(run_id: str, payment_id: str):
        with db.transaction(run_id) as tx:
            payment = tx.payment(payment_id)
            if payment is None:
                raise DomainError("PAYMENT_NOT_FOUND")
            result = {
                "payment": payment,
                "events": tx.rows(
                    "SELECT * FROM events WHERE run_id=:r AND aggregate_id=:p ORDER BY seq LIMIT 500",
                    p=payment_id,
                ),
                "reservations": tx.rows(
                    "SELECT * FROM reservations WHERE run_id=:r AND payment_id=:p ORDER BY id", p=payment_id
                ),
                "journal": tx.rows(
                    """SELECT j.id,j.currency,l.account_id,l.delta FROM journal_entries j JOIN journal_lines l
                       ON j.run_id=l.run_id AND j.id=l.entry_id WHERE j.run_id=:r AND j.payment_id=:p ORDER BY j.id,l.line_no""",
                    p=payment_id,
                ),
            }
            if payment.get("rail") == "CORRESPONDENT":
                chain = chain_inspection(tx, payment_id)
                result.update({"chain": chain, "hops": chain["hops"], "obligations": chain["obligations"]})
            return result

    @app.get("/api/v1/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return db.events(run_id, after, limit)

    @app.get("/api/v1/runs/{run_id}/network")
    def network(run_id: str):
        with db.transaction(run_id) as tx:
            result = {
                "banks": BANKS,
                "implemented_rails": ["ATOMIC_PVP"],
                "positions": tx.rows("""SELECT a.id,a.owner,a.currency,a.kind,a.issuer,p.posted,p.reserved,p.buffer,
                      p.posted-p.reserved-p.buffer spendable FROM accounts a JOIN positions p
                      ON a.run_id=p.run_id AND a.id=p.account_id WHERE a.run_id=:r ORDER BY a.id"""),
                "faults": tx.rows("SELECT * FROM faults WHERE run_id=:r ORDER BY id"),
                "links": [
                    {"from": "ATLAS_US", "to": "STERLING_UK", "rail": "ATOMIC_PVP"},
                    {"from": "STERLING_UK", "to": "MERIDIAN_SG", "rail": "ATOMIC_PVP"},
                ],
            }
            if tx.run["config"].get("rail") == "CORRESPONDENT":
                relationships = tx.rows("""SELECT r.id,r.respondent,r.correspondent,r.currency,r.config,
                n.posted nostro_signed,v.posted vostro_minor,v.reserved FROM cb_relationships r
                JOIN positions n ON r.run_id=n.run_id AND r.nostro_id=n.account_id
                JOIN positions v ON r.run_id=v.run_id AND r.vostro_id=v.account_id WHERE r.run_id=:r ORDER BY r.id""")
                result["implemented_rails"] = ["CORRESPONDENT"]
                result["relationships"] = relationships
                result["links"] = [
                    {
                        "from": r["respondent"],
                        "to": r["correspondent"],
                        "rail": "CORRESPONDENT",
                        "currency": r["currency"],
                        "relationship_id": r["id"],
                    }
                    for r in relationships
                ]
            return result

    @app.get("/api/v1/runs/{run_id}/ledger")
    def ledger(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        with db.transaction(run_id) as tx:
            return tx.rows(
                """SELECT j.id,j.payment_id,j.currency,l.account_id,l.delta FROM journal_entries j
              JOIN journal_lines l ON j.run_id=l.run_id AND j.id=l.entry_id
              WHERE j.run_id=:r ORDER BY j.id,l.line_no LIMIT :n OFFSET :o""",
                n=limit,
                o=offset,
            )

    @app.get("/api/v1/runs/{run_id}/metrics")
    def run_metrics(run_id: str):
        return metrics(db, run_id)

    @app.post("/api/v1/runs/{run_id}/reconcile")
    def reconciliation(run_id: str):
        return reconcile(db, run_id)

    @app.post("/api/v1/runs/{run_id}/replay-check")
    def replay(run_id: str):
        return replay_check(db, run_id)

    @app.post("/api/v1/runs/{run_id}/faults")
    def inject(run_id: str, fault: Fault):
        with db.transaction(run_id) as tx:
            acquired = tx.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(:r,0))").scalar()
            if not acquired:
                raise DomainError("RUN_BUSY")
            runtime = tx.one("SELECT * FROM runtime WHERE run_id=:r")
            if runtime is None or not runtime["paused"]:
                raise DomainError("PAUSE_BEFORE_INJECTION")
            if fault.at_ms <= tx.run["logical_time_ms"]:
                raise DomainError("FAULT_MUST_BE_FUTURE")
            queue, cursor = runtime["queue"], runtime["cursor"]
            if sum(e[3] == "FAULT_ON" for e in queue) >= 100:
                raise DomainError("FAULT_LIMIT")
            if any(e[4].get("id") == fault.id for e in queue if e[3].startswith("FAULT_")):
                raise DomainError("FAULT_ID_REUSED")
            sequence = max(e[2] for e in queue) + 1
            pending = queue[cursor:] + [
                [fault.at_ms, 0, sequence, "FAULT_ON", fault.model_dump()],
                [fault.at_ms + fault.duration_ms, 0, sequence + 1, "FAULT_OFF", fault.model_dump()],
            ]
            pending.sort(key=lambda x: x[:3])
            tx.execute(
                "UPDATE runtime SET queue=CAST(:q AS jsonb) WHERE run_id=:r",
                q=canonical(queue[:cursor] + pending),
            )
            tx.event("FAULT_SCHEDULED", fault.id, fault.model_dump())
        return {"scheduled": fault.id, "at_ms": fault.at_ms}

    @app.websocket("/api/v1/runs/{run_id}/stream")
    async def stream(websocket: WebSocket, run_id: str):
        await websocket.accept()
        after = 0
        try:
            while True:
                committed = await asyncio.to_thread(db.events, run_id, after, 200)
                if committed:
                    after = committed[-1]["seq"]
                # Empty heartbeat also observes closed clients rather than polling forever.
                await websocket.send_json({"schema_version": 1, "events": committed, "cursor": after})
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            return

    @app.get("/api/v1/runs/{run_id}/export")
    def export(run_id: str):
        # Bounded metadata export; full event/journal export is available through CLI.
        with db.transaction(run_id) as tx:
            config = tx.run
        return {"run": config, "metrics": metrics(db, run_id), "verification": reconcile(db, run_id)}

    from railstorm.rails.instant_link import InstantLink

    instant = InstantLink(db)

    class InstantRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        command: PaymentCommand
        provider_sgd: StrictInt = Field(default=50000, ge=0, le=1000000)
        source_available: bool = True

    class DeliveryRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        at_ms: StrictInt = Field(ge=0, le=86400000)
        available: bool = True
        lose_ack: bool = False

    @app.post("/api/v1/instant-link/payments")
    def instant_create(request: InstantRequest):
        run = "il-" + uuid.uuid4().hex
        instant.initialize(run, request.provider_sgd, source_available=request.source_available)
        payment = instant.prepare(run, request.command)
        return {"run_id": run, "payment_id": payment["id"], "payment": payment}

    @app.post("/api/v1/instant-link/{run_id}/submit")
    def instant_submit(run_id: str, request: SubmitRequest):
        return instant.prepare(run_id, request.command)

    @app.get("/api/v1/instant-link/{run_id}/recoverable")
    def instant_recoverable(run_id: str):
        return instant.recoverable(run_id)

    @app.post("/api/v1/instant-link/{run_id}/{payment}/deliver/{stage}")
    def instant_deliver(run_id: str, payment: str, stage: str, request: DeliveryRequest):
        return instant.deliver(run_id, payment, stage, request.at_ms, request.available, request.lose_ack)

    @app.post("/api/v1/instant-link/{run_id}/{payment}/recover")
    def instant_recover(run_id: str, payment: str, request: DeliveryRequest):
        return instant.recover(run_id, payment, request.at_ms, request.available)

    @app.get("/api/v1/instant-link/fixtures")
    def instant_fixtures():
        from railstorm.instant_fixtures import IL_FIXTURES

        return IL_FIXTURES

    @app.post("/api/v1/instant-link/fixtures/{fixture}")
    def instant_fixture(fixture: str):
        from railstorm.instant_fixtures import execute_fixture

        return execute_fixture(db, fixture)

    @app.post("/api/v1/instant-link/golden")
    def instant_golden():
        run = "il-" + uuid.uuid4().hex
        instant.initialize(run)
        command = PaymentCommand(
            idempotency_key="golden", principal_minor=10000, requested_rail="INSTANT_LINK"
        )
        instant.prepare(run, command)
        return {"run_id": run, "payment_id": "golden"}

    @app.post("/api/v1/instant-link/{run_id}/{payment}/advance")
    def instant_advance(
        run_id: str,
        payment: str,
        at_ms: int = Query(ge=0),
        destination_available: bool = True,
        drop_ack: bool = False,
    ):
        return instant.advance(run_id, payment, at_ms, destination_available, drop_ack)

    @app.get("/api/v1/instant-link/{run_id}/{payment}")
    def instant_inspect(run_id: str, payment: str):
        return instant.inspect(run_id, payment)

    @app.get("/api/v1/instant-link/{run_id}/{payment}/verification")
    def instant_verify(run_id: str, payment: str):
        instant.inspect(run_id, payment)
        return reconcile(db, run_id)

    from railstorm.routing import Intent, Network, Policy, Router
    from railstorm.routing_experiments import experiment

    router = Router(db)

    class RoutingRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        intent: Intent
        network: Network
        policy: Policy = Field(default_factory=Policy)
        at_ms: StrictInt = Field(default=0, ge=0, le=86400000)

    @app.post("/api/v1/routing/evaluations")
    def routing_evaluate(request: RoutingRequest):
        run = "routing-" + uuid.uuid4().hex
        router.initialize(run)
        record = router.evaluate(run, request.intent, request.network, request.at_ms, request.policy)
        return {"run_id": run, "record": record}

    @app.get("/api/v1/routing/{run_id}/{payment}")
    def routing_read(run_id: str, payment: str):
        return router.read(run_id, payment)

    @app.post("/api/v1/routing/{run_id}/{payment}/execute")
    def routing_execute(run_id: str, payment: str, request: VirtualTimeRequest):
        return router.execute(run_id, payment, request.at_ms)

    @app.get("/api/v1/routing/{run_id}/{payment}/status")
    def routing_status(run_id: str, payment: str):
        return router.status(run_id, payment)

    @app.get("/api/v1/routing/{run_id}/{payment}/verification")
    def routing_verification(run_id: str, payment: str):
        return router.reconcile(run_id, payment)

    @app.post("/api/v1/routing/{run_id}/{payment}/recover")
    def routing_recover(run_id: str, payment: str, request: VirtualTimeRequest):
        return router.recover(run_id, payment, request.at_ms)

    @app.post("/api/v1/routing/correspondent/{case}")
    def routing_correspondent_example(case: str):
        from railstorm.correspondent_routing_examples import example

        return example(db, case)

    @app.post("/api/v1/routing/{run_id}/{payment}/restore")
    def routing_restore(run_id: str, payment: str, request: VirtualTimeRequest):
        from railstorm.correspondent_routing_examples import restore

        return restore(db, run_id, payment, request.at_ms)

    @app.post("/api/v1/routing/experiments/{scenario}")
    def routing_experiment(scenario: str):
        if scenario not in {"RT-01", "RT-02", "RT-03", "RT-04", "RT-05", "RT-06"}:
            raise DomainError("EXPERIMENT_NOT_FOUND")
        return experiment(db, scenario)

    from railstorm.optimizer_experiments import ARTIFACTS, SCENARIOS, compare

    optimizer_lock = threading.Lock()

    class OptimizerRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        include_v2: bool = False
        scenario: str = Field(pattern=r"^OPT-(0[1-9]|10)$")
        seed: StrictInt = Field(default=71, ge=0, le=1000000000)
        count: StrictInt = Field(default=5, ge=1, le=5)

    @app.get("/api/v1/optimizer/scenarios")
    def optimizer_scenarios():
        return SCENARIOS

    @app.post("/api/v1/optimizer/comparisons")
    def optimizer_comparison(request: OptimizerRequest):
        with optimizer_lock:
            return compare(db, request.scenario, request.seed, request.count, include_v2=request.include_v2)

    @app.get("/api/v1/optimizer/comparisons/{identifier}")
    def optimizer_artifact(identifier: str):
        import json
        import re

        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise DomainError("COMPARISON_NOT_FOUND")
        path = ARTIFACTS / (identifier + ".json")
        if not path.is_file():
            raise DomainError("COMPARISON_NOT_FOUND")
        return json.loads(path.read_text(encoding="utf-8"))

    from experiments.p2a_campaign import OUT as CHAOS_ARTIFACTS
    from experiments.p2a_campaign import REGISTRY as CHAOS_REGISTRY
    from experiments.p2a_campaign import execute as execute_chaos

    class ChaosRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        seed: StrictInt = Field(default=20261030, ge=0, le=1000000000)
        count: StrictInt = Field(default=10, ge=1, le=10)

    @app.get("/api/v1/chaos/scenarios")
    def chaos_scenarios():
        return {
            key: {"name": value[0], "kind": value[1], "variant": value[2]}
            for key, value in CHAOS_REGISTRY.items()
        }

    @app.post("/api/v1/chaos/scenarios/{scenario}/runs")
    def chaos_run(scenario: str, request: ChaosRequest):
        import json

        if scenario not in CHAOS_REGISTRY:
            raise DomainError("CHAOS_SCENARIO_NOT_FOUND")
        result = execute_chaos(db, scenario, request.seed, request.count)
        identifier = uuid.uuid4().hex
        CHAOS_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        path = CHAOS_ARTIFACTS / ("browser-" + identifier + ".json")
        with path.open("x", encoding="utf-8") as file:
            json.dump(result, file, indent=2)
        return {
            "id": identifier,
            "scenario": scenario,
            "status": result["status"],
            "manifest_hash": result["manifest_hash"],
            "runs": result["payload"]["runs"],
        }

    @app.get("/api/v1/chaos/runs/{identifier}")
    def chaos_read(identifier: str):
        import json
        import re

        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise DomainError("CHAOS_RUN_NOT_FOUND")
        path = CHAOS_ARTIFACTS / ("browser-" + identifier + ".json")
        if not path.is_file():
            raise DomainError("CHAOS_RUN_NOT_FOUND")
        result = json.loads(path.read_text(encoding="utf-8"))
        return {
            "id": identifier,
            "manifest": result["manifest"],
            "stages": result["payload"]["stages"],
            "final": result["payload"]["final"],
            "durable_financial_state": result["durable_financial_state"],
            "audit": result["audit"],
            "runtime_seconds": result["runtime_seconds"],
        }

    dist = Path(__file__).parents[1] / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="command-center")
    return app


def factory():
    return create_app()
