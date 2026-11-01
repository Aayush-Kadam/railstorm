"""Persisted deterministic virtual queue and fault-driven atomic PvP experiments.

Only ATOMIC_PVP is accepted in this checkpoint. Router comparisons explicitly
report one candidate, so they cannot establish liquidity-aware routing benefit.
"""

import random
import time

from sqlalchemy import text

from railstorm.audit import reconcile
from railstorm.domain import DomainError, PaymentCommand, Scheduler, canonical, digest
from railstorm.ledger import Ledger
from railstorm.scenarios import Fault, Scenario
from railstorm.store import Store


def compile_queue(scenario: Scenario):
    rng = random.Random(scenario.seed)
    queue = Scheduler()
    for fault in scenario.faults:
        queue.push(fault.at_ms, 0, "FAULT_ON", fault.model_dump())
        queue.push(fault.at_ms + fault.duration_ms, 0, "FAULT_OFF", fault.model_dump())
    for index in range(scenario.payment_count):
        at = index * scenario.interval_ms
        amount = scenario.principal_minor + rng.randint(-scenario.jitter_minor, scenario.jitter_minor)
        payload = {"index": index, "amount": amount}
        queue.push(at, 1, "ARRIVAL", payload)
        queue.push(at + scenario.settlement_delay_ms, 2, "SETTLE", payload)
        # Fixed recovery schedule shared by all policies; no future information is given to a router.
        queue.push(at + scenario.settlement_delay_ms + 1000, 3, "RECOVER", payload)
        queue.push(at + scenario.deadline_ms, 4, "EXPIRY", payload)
    return [queue.pop() for _ in range(len(queue.queue))]


def create_run(store: Store, run_id: str, scenario: Scenario):
    Ledger(store).initialize(run_id, scenario.model_dump())
    with store.transaction(run_id) as tx:
        tx.execute(
            "INSERT INTO runtime(run_id,queue) VALUES(:r,CAST(:q AS jsonb))",
            q=canonical(compile_queue(scenario)),
        )
        tx.event(
            "RUN_READY", "run", {"config_fingerprint": digest(scenario.model_dump()), "seed": scenario.seed}
        )


def apply_fault(store: Store, run_id: str, fault: Fault, active: bool, at_ms: int):
    with store.transaction(run_id) as tx:
        old = tx.one("SELECT * FROM faults WHERE run_id=:r AND id=:id", id=fault.id)
        if old and old["active"] == active:
            return
        if old and old["data"] != fault.model_dump():
            raise DomainError("FAULT_ID_REUSED")
        tx.time(at_ms)
        frozen = old["frozen_minor"] if old else 0
        if fault.type == "LIQUIDITY_FREEZE":
            account = f"{fault.target}:{fault.currency}"
            position = tx.position(account)
            if active:
                frozen = min(
                    fault.amount_minor, position["posted"] - position["reserved"] - position["buffer"]
                )
                buffer = position["buffer"] + frozen
            else:
                buffer = position["buffer"] - frozen
            tx.execute(
                "UPDATE positions SET buffer=:b WHERE run_id=:r AND account_id=:a", b=buffer, a=account
            )
            tx.event("BUFFER_CHANGED", account, {"account_id": account, "buffer": buffer})
        tx.execute(
            """INSERT INTO faults VALUES(:r,:id,CAST(:d AS jsonb),:a,:f)
        ON CONFLICT(run_id,id) DO UPDATE SET active=excluded.active,frozen_minor=excluded.frozen_minor""",
            id=fault.id,
            d=canonical(fault.model_dump()),
            a=active,
            f=frozen,
        )
        tx.invalidate_faults()
        tx.event(
            "FAULT_ACTIVATED" if active else "FAULT_RESTORED",
            fault.id,
            {"fault": fault.model_dump(), "frozen_minor": frozen},
        )


def set_paused(store: Store, run_id: str, paused: bool):
    with store.transaction(run_id) as tx:
        tx.execute("UPDATE runtime SET paused=:p WHERE run_id=:r", p=paused)
        tx.execute("UPDATE runs SET status=:s WHERE id=:r", s="PAUSED" if paused else "RUNNING")


def step(store: Store, run_id: str, count: int = 1):
    with store.engine.connect() as connection:
        rail = connection.execute(
            text("SELECT config->>'rail' FROM runs WHERE id=:r"), {"r": run_id}
        ).scalar()
    if rail == "CORRESPONDENT":
        from railstorm.cb_simulation import step_correspondent

        return step_correspondent(store, run_id, count)
    # Queue cursor and every economic operation in the batch commit together.
    # Nested Ledger commands enlist in this transaction; a worker crash rolls
    # back the entire batch, so restarting cannot skip or reorder an event.
    with store.transaction(run_id):
        return _step(store, run_id, count)


def _step(store: Store, run_id: str, count: int = 1):
    if not 1 <= count <= 1000:
        raise DomainError("STEP_LIMIT")
    # Session advisory lock prevents two API/CLI workers from advancing one queue concurrently.
    # It is not part of financial correctness: Ledger independently locks each run.
    with store.engine.connect() as guard:
        locked = guard.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:r,0))"), {"r": run_id}
        ).scalar()
        if not locked:
            raise DomainError("RUN_BUSY")
        try:
            with store.transaction(run_id) as tx:
                runtime = tx.one("SELECT * FROM runtime WHERE run_id=:r")
                if runtime is None:
                    raise DomainError("NO_SIMULATION_QUEUE")
                queue = runtime["queue"]
                cursor = runtime["cursor"]
            ledger = Ledger(store)
            for index in range(cursor, min(cursor + count, len(queue))):
                at_ms, _, _, kind, payload = queue[index]
                payment = f"p{payload.get('index', 0):06d}"
                if kind.startswith("FAULT_"):
                    apply_fault(store, run_id, Fault.model_validate(payload), kind == "FAULT_ON", at_ms)
                else:
                    with store.transaction(run_id) as tx:
                        active = [row["data"] for row in tx.active_faults()]
                        config = tx.run["config"]
                        tx.time(at_ms)
                    if kind == "ARRIVAL":
                        cmd = PaymentCommand(
                            idempotency_key=payment,
                            principal_minor=payload["amount"],
                            max_fee_minor=config["fee_minor"],
                            deadline_ms=config["deadline_ms"],
                        )
                        result = ledger.prepare(run_id, cmd, at_ms)
                        with store.transaction(run_id) as tx:
                            tx.event(
                                "ROUTE_SELECTED",
                                payment,
                                {
                                    "policy": config["routing_policy"],
                                    "chosen": "ATOMIC_PVP",
                                    "candidate_count": 1,
                                    "expected_fee_minor": config["fee_minor"],
                                    "reference_currency": "USD",
                                    "expected_delay_ms": config["settlement_delay_ms"],
                                    "reason": "ONLY_IMPLEMENTED_RAIL",
                                    "outcome": result["economic_status"],
                                },
                            )
                        duplicates = sum(f["count"] for f in active if f["type"] == "DUPLICATE_DELIVERY")
                        for _ in range(duplicates):
                            ledger.prepare(run_id, cmd, at_ms)
                        if duplicates:
                            with store.transaction(run_id) as tx:
                                tx.event("DUPLICATES_PREVENTED", payment, {"attempts": duplicates})
                    elif kind in ("SETTLE", "RECOVER", "EXPIRY"):
                        result = ledger.settle(run_id, payment, at_ms)
                        drops = [f for f in active if f["type"] in ("MESSAGE_DROP", "CRASH_AFTER_COMMIT")]
                        if result["economic_status"] == "SETTLED" and result["ack_status"] != "ACKNOWLEDGED":
                            if not drops:
                                ledger.acknowledge(run_id, payment)
                            elif kind == "SETTLE":
                                with store.transaction(run_id) as tx:
                                    tx.event(
                                        "ACK_DROPPED",
                                        payment,
                                        {"fault_ids": [f["id"] for f in drops], "economic_status": "SETTLED"},
                                    )
            with store.transaction(run_id) as tx:
                # The entire batch was already one transaction. Only its final
                # cursor/time can ever be observed after commit.
                tx.execute("UPDATE runtime SET cursor=:c WHERE run_id=:r", c=min(cursor + count, len(queue)))
                runtime = tx.one("SELECT cursor,paused,error FROM runtime WHERE run_id=:r")
                done = runtime["cursor"] == len(queue)
                if done:
                    tx.execute("UPDATE runs SET status='COMPLETED' WHERE id=:r")
                return {**runtime, "total_events": len(queue), "done": done}
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(hashtextextended(:r,0))"), {"r": run_id})


def metrics(store: Store, run_id: str):
    with store.transaction(run_id) as tx:
        if tx.run["config"].get("rail") == "CORRESPONDENT":
            from railstorm.cb_simulation import cb_metrics

            return cb_metrics(tx)
        payments = [r["data"] for r in tx.rows("SELECT data FROM payments WHERE run_id=:r")]
        statuses: dict[str, int] = {}
        for payment in payments:
            key = payment["economic_status"]
            statuses[key] = statuses.get(key, 0) + 1
        settled = [p for p in payments if p["economic_status"] == "SETTLED"]
        latencies = sorted(p["settled_ms"] - p["submitted_ms"] for p in settled)

        def percentile(p):
            return latencies[max(0, (len(latencies) * p + 99) // 100 - 1)] if latencies else None

        before_deadline = sum(p["settled_ms"] <= p["deadline_ms"] for p in settled)
        ineligible = sum(p["reason"] in ("UNSUPPORTED_CORRIDOR", "RAIL_NOT_IMPLEMENTED") for p in payments)
        eligible = len(payments) - ineligible
        fees_by_currency: dict[str, int] = {}
        for payment in settled:
            currency = payment["quote"]["fee_currency"]
            fees_by_currency[currency] = fees_by_currency.get(currency, 0) + payment["quote"]["fee_minor"]
        return {
            "submitted": len(payments),
            "eligible": eligible,
            "ineligible": ineligible,
            "settled": len(settled),
            "before_deadline": before_deadline,
            "success_fraction": {"numerator": before_deadline, "denominator": eligible},
            "statuses": statuses,
            "pending": sum(p["economic_status"] == "PREPARED" for p in payments),
            "missing_ack": sum(p["ack_status"] == "MISSING" for p in payments),
            "fees_by_currency_minor": fees_by_currency,
            "fees_usd_minor": fees_by_currency.get("USD", 0) if set(fees_by_currency) <= {"USD"} else None,
            "median_ms": percentile(50),
            "p95_ms": percentile(95),
            "p99_ms": percentile(99),
            "latency_population": "SETTLED_ONLY; rejected and unfinished outcomes reported separately",
            "exposure": 0,
            "exposure_scope": "ATOMIC_PVP only",
            "peak_reserved": None,
            "memory_bytes": None,
            "fx_spread_cost": None,
            "candidate_rails": ["ATOMIC_PVP"],
        }


def run_scenario(store: Store, run_id: str, scenario: Scenario):
    start = time.perf_counter()
    create_run(store, run_id, scenario)
    while not step(store, run_id, 1000)["done"]:
        pass
    execution = time.perf_counter() - start
    audit_start = time.perf_counter()
    report = reconcile(store, run_id)
    return {
        "run_id": run_id,
        "config_fingerprint": digest(scenario.model_dump()),
        "execution_wall_seconds": execution,
        "audit_wall_seconds": time.perf_counter() - audit_start,
        "metrics": metrics(store, run_id),
        "verification": report,
    }


def compare(store: Store, prefix: str, scenario: Scenario):
    results = {}
    for policy in ("COST_FIRST", "LIQUIDITY_AWARE"):
        results[policy] = run_scenario(
            store, f"{prefix}-{policy}", scenario.model_copy(update={"routing_policy": policy})
        )
    return {
        "limitation": "Single candidate rail; this is a matched determinism control, not a routing efficacy experiment.",
        "results": results,
    }
