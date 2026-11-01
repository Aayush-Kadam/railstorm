"""Controlled small online policy comparisons over the existing financial engines."""

import heapq
import json
import math
import random
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sqlalchemy import event

from railstorm.audit import empty_state, reconcile, reduce_event, snapshot
from railstorm.domain import Currency, DomainError, digest
from railstorm.ledger import Ledger
from railstorm.rails.correspondent import CorrespondentRail, default_relationships
from railstorm.rails.instant_link import InstantLink
from railstorm.routing import Intent, Network, Policy, Rail, Router
from railstorm.scenarios import Fault
from railstorm.simulation import apply_fault

SCENARIOS = {
    f"OPT-{i:02d}": name
    for i, name in enumerate(
        [
            "Healthy",
            "Destination SGD shortage",
            "Correspondent bottleneck",
            "Instant cap",
            "Participant outage",
            "Quote availability proxy; synchronized rate repricing UNSUPPORTED",
            "Mixed retail/wholesale",
            "Liquidity concentration",
            "Delayed settlement",
            "Adversarial safety",
        ],
        1,
    )
}
_active_probe: ContextVar[Any] = ContextVar("optimizer_sql_probe", default=None)
VERSIONS = ("fixed-order-v1", "LIQUIDITY_AWARE_V1")
ARTIFACTS = Path(__file__).resolve().parents[1] / "experiments" / "reports" / "optimizer"


def specification(scenario, seed=71, count=5):
    if scenario not in SCENARIOS or not 1 <= count <= 100:
        raise ValueError("INVALID_COMPARISON")
    rng = random.Random(seed)
    source = 200000 if scenario == "OPT-01" else 1000000 if scenario in ("OPT-04", "OPT-07") else 20000
    target = (
        500000
        if scenario == "OPT-01"
        else 1000000
        if scenario == "OPT-04"
        else 200000
        if scenario == "OPT-07"
        else 20000
        if scenario == "OPT-02"
        else 5000
        if scenario == "OPT-08"
        else 50000
    )
    cb_target = (
        20000
        if scenario == "OPT-02"
        else 100
        if scenario == "OPT-03"
        else 50000
        if scenario == "OPT-08"
        else 2000000
    )
    work = []
    for i in range(count):
        amount = (
            150000
            if scenario == "OPT-04"
            else 150000
            if scenario == "OPT-07" and i % 3 == 1
            else 5000
            if scenario == "OPT-07" and i % 3 == 2
            else 10000
        ) + rng.randint(-50, 50)
        work.append(
            {
                "id": f"p{i:04d}",
                "at_ms": i * 100,
                "amount_minor": amount,
                "source": "USD",
                "target": "USD" if scenario == "OPT-07" and i % 3 == 2 else "SGD",
                "payment_class": "WHOLESALE" if amount > 100000 else "RETAIL",
                "finality": "ATOMIC_REQUIRED"
                if scenario == "OPT-07" and i % 3 == 2
                else "INDEPENDENT_STAGES_ALLOWED",
            }
        )
    faults = []
    if scenario == "OPT-05":
        faults = [
            {"kind": "PARTICIPANT_DOWN", "id": "rhine-outage", "bank": "RHINE_EU", "start": 120, "end": 350}
        ]
    if scenario == "OPT-09":
        faults = [
            {
                "kind": "PARTICIPANT_DOWN",
                "id": "destination-outage",
                "bank": "MERIDIAN_SG",
                "start": 20,
                "end": 1500,
            }
        ]
    if scenario == "OPT-06":
        faults = [{"kind": "QUOTE_UNAVAILABLE", "id": "quote-gap", "start": 150, "end": 350}]
    if scenario == "OPT-10":
        faults = [
            {"kind": "ACCOUNT_FREEZE", "id": "sgd-shock", "start": 15, "end": 250, "amount_minor": 10000},
            {
                "kind": "PARTICIPANT_DOWN",
                "id": "rhine-interruption",
                "bank": "RHINE_EU",
                "start": 80,
                "end": 240,
            },
        ]
    return {
        "protocol": "optimizer-o0-v1",
        "scenario": scenario,
        "seed": seed,
        "count": count,
        "horizon_ms": max(1200, count * 100 + 500),
        "source_funding_minor": source,
        "target_funding_minor": target,
        "cb_target_funding_minor": cb_target,
        "atomic_fee_minor": 25,
        "quote_process": "fixed133/100; observed availability only",
        "workload": work,
        "faults": faults,
        "deferral": "NONE; no-route is rejected, not indefinitely queued",
        "coverage": "PARTIALLY_SUPPORTED: quote availability only; synchronized rate change unsupported"
        if scenario == "OPT-06"
        else "SUPPORTED",
        "fault_information_to_policy": "current observations only",
    }


class Telemetry:
    def __init__(self, store, worlds):
        self.store = store
        self.worlds = worlds
        self.cursor = {r: 0 for r in worlds}
        self.projections = {r: empty_state() for r in worlds}
        self.peak = {}
        self.reservation_peaks = {}
        self.obligation_peak = {}
        self.obligation_area = {}
        self.obligation_totals = {}
        self.last_time = 0
        self.sql_seconds = 0.0
        self.sql_count = 0

    def before(self, conn, cursor, statement, parameters, context, executemany):
        context._optimizer_started = time.perf_counter() if _active_probe.get() is self else None

    def after(self, conn, cursor, statement, parameters, context, executemany):
        if context._optimizer_started is None:
            return
        self.sql_seconds += time.perf_counter() - context._optimizer_started
        self.sql_count += 1

    def collect(self, rail):
        while True:
            rows = self.store.events(self.worlds[rail], self.cursor[rail], 512)
            if not rows:
                break
            for row in rows:
                self.integrate(row["logical_time_ms"])
                reduce_event(self.projections[rail], row)
                if row["event_type"] in {"PAYMENT_CHANGED", "CB_OBLIGATION_CHANGED"}:
                    amounts: dict[str, int] = {}
                    for state in self.projections.values():
                        for payment in state["payments"].values():
                            obligation = payment.get("obligation")
                            if obligation:
                                c = obligation["currency"]
                                amounts[c] = amounts.get(c, 0) + obligation["amount_minor"]
                        for obligation in state.get("correspondent", {}).get("obligations", {}).values():
                            if obligation["status"] == "OPEN":
                                c = obligation["currency"]
                                amounts[c] = amounts.get(c, 0) + obligation["amount"]
                    self.obligation_totals = amounts
                    for c, n in amounts.items():
                        self.obligation_peak[c] = max(self.obligation_peak.get(c, 0), n)
                self.cursor[rail] = row["seq"]
                if row["event_type"] == "RESERVATION_CHANGED":
                    totals: dict[str, int] = {}
                    for r, state in self.projections.items():
                        for account, position in state["positions"].items():
                            currency = state["accounts"][account]["currency"]
                            n = position["reserved"]
                            totals[currency] = totals.get(currency, 0) + n
                            key = r + "/" + account
                            self.reservation_peaks[key] = max(self.reservation_peaks.get(key, 0), n)
                    for c, n in totals.items():
                        self.peak[c] = max(self.peak.get(c, 0), n)

    def integrate(self, at_ms):
        assert at_ms >= self.last_time, "Financial scheduler moved backwards"
        for c, n in self.obligation_totals.items():
            self.obligation_area[c] = self.obligation_area.get(c, 0) + n * (at_ms - self.last_time)
        self.last_time = at_ms


def _payment(store, world, key):
    with store.transaction(world) as tx:
        return tx.payment(key)


def _history(store, world):
    rows: list[dict] = []
    while True:
        page = store.events(world, rows[-1]["seq"] if rows else 0, 512)
        if not page:
            return rows
        rows.extend(page)


def _normalize(value, aliases):
    if isinstance(value, dict):
        return {k: _normalize(v, aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, aliases) for v in value]
    return aliases.get(value, value) if isinstance(value, str) else value


def run_policy(store, spec, version):
    wall = time.perf_counter()
    run = "opt-" + uuid.uuid4().hex
    worlds: dict[Rail, str] = {
        r: run + "-" + r.lower() for r in ("ATOMIC_PVP", "CORRESPONDENT", "INSTANT_LINK")
    }
    funding = {
        "ATLAS_US:USD": spec["source_funding_minor"],
        "STERLING_UK:USD": 0,
        "STERLING_UK:SGD": spec["target_funding_minor"],
        "MERIDIAN_SG:SGD": 0,
    }
    Ledger(store).initialize(worlds["ATOMIC_PVP"], {"fee_minor": 25, "funding": funding})
    rels = [
        r.model_copy(update={"funding_minor": spec["cb_target_funding_minor"]})
        if r.id in ("SR_SGD", "RM_SGD")
        else r
        for r in default_relationships()
    ]
    CorrespondentRail(store).initialize(worlds["CORRESPONDENT"], rels)
    InstantLink(store).initialize(
        worlds["INSTANT_LINK"],
        provider_sgd=spec["target_funding_minor"],
        sender_usd=spec["source_funding_minor"],
    )
    router = Router(store)
    router.initialize(run)
    policy = Policy(version=version)
    initial = {}
    for r, w in worlds.items():
        with store.transaction(w) as tx:
            initial[r] = snapshot(tx)
    probe = Telemetry(store, worlds)
    for r in worlds:
        probe.collect(r)
    probe_token = _active_probe.set(probe)
    event.listen(store.engine, "before_cursor_execute", probe.before)
    event.listen(store.engine, "after_cursor_execute", probe.after)
    queue: list[Any] = []
    sequence = 0
    down: set[str] = set()
    quote_available = True
    frozen: dict[tuple[str, str], int] = {}
    receipts = {}
    decisions = []
    recovery_attempts = 0
    progress_attempts = 0
    algo_ns = 0
    decision_db_seconds = 0.0
    freeze_observations = []

    def push(t, priority, kind, payload):
        nonlocal sequence
        sequence += 1
        heapq.heappush(queue, (t, priority, sequence, kind, payload))

    for fault in spec["faults"]:
        push(fault["start"], 0, "FAULT_ON", fault)
        push(fault["end"], 0, "FAULT_OFF", fault)
    for demand in spec["workload"]:
        push(demand["at_ms"], 1, "ARRIVAL", demand)
    actions = 0
    try:
        while queue:
            t, priority, serial, kind, payload = heapq.heappop(queue)
            if t > spec["horizon_ms"]:
                break
            actions += 1
            if actions > 10000:
                raise RuntimeError("ACTION_LIMIT; no success claim")
            if kind.startswith("FAULT_"):
                active = kind == "FAULT_ON"
                f = payload
                if f["kind"] == "PARTICIPANT_DOWN":
                    (down.add if active else down.discard)(f["bank"])
                    model = Fault(
                        id=f["id"],
                        type="PARTICIPANT_DOWN",
                        target=f["bank"],
                        at_ms=f["start"],
                        duration_ms=f["end"] - f["start"],
                    )
                    for r, w in worlds.items():
                        apply_fault(store, w, model, active, t)
                        probe.collect(r)
                elif f["kind"] == "QUOTE_UNAVAILABLE":
                    quote_available = not active
                else:
                    for r, w in worlds.items():
                        account = "VOSTRO:SR_SGD" if r == "CORRESPONDENT" else "STERLING_UK:SGD"
                        with store.transaction(w) as tx:
                            tx.time(t)
                            pos = tx.position(account)
                            amount = (
                                min(f["amount_minor"], pos["posted"] - pos["reserved"] - pos["buffer"])
                                if active
                                else frozen[(r, f["id"])]
                            )
                            if active:
                                frozen[(r, f["id"])] = amount
                            buffer = pos["buffer"] + amount if active else pos["buffer"] - amount
                            tx.execute(
                                "UPDATE positions SET buffer=:b WHERE run_id=:r AND account_id=:a",
                                b=buffer,
                                a=account,
                            )
                            tx.event(
                                "BUFFER_CHANGED",
                                account,
                                {"account_id": account, "buffer": buffer, "shock_id": f["id"]},
                            )
                            freeze_observations.append(
                                {
                                    "rail": r,
                                    "at_ms": t,
                                    "account": account,
                                    "requested_minor": f["amount_minor"],
                                    "frozen_minor": amount,
                                    "active": active,
                                }
                            )
                        probe.collect(r)
                continue
            if kind == "ARRIVAL":
                d = payload
                key = d["id"]
                intent = Intent(
                    payment_id=key,
                    idempotency_key=key,
                    source_currency=Currency(d["source"]),
                    destination_currency=Currency(d["target"]),
                    amount_minor=d["amount_minor"],
                    payment_class=d["payment_class"],
                    finality=d["finality"],
                    created_ms=t,
                    execute_from_ms=t,
                    execute_until_ms=spec["horizon_ms"] + 1000,
                    maximum_fees_by_currency={Currency.USD: 1000, Currency.SGD: 1000},
                )
                network = Network(
                    worlds=worlds,
                    unavailable=tuple(sorted(down)),
                    quote_available=quote_available,
                    quote_expires_ms=spec["horizon_ms"] + 1000,
                )
                started = time.perf_counter()
                record = router.evaluate(run, intent, network, t, policy)
                decision_db_seconds += time.perf_counter() - started
                from railstorm.optimizer import choose

                tick = time.perf_counter_ns()
                selected, _ = choose(record["evaluation"]["candidates"], policy)
                algo_ns += time.perf_counter_ns() - tick
                assert selected == record["evaluation"]["selected"]
                decisions.append(record["evaluation"])
                receipts[key] = {"arrival_ms": t, "selected": selected, "execution_error": None}
                if not selected:
                    continue
                try:
                    router.execute(run, key, t, prepare_only=True)
                except DomainError as exc:
                    if exc.code not in ("EXECUTION_PREFLIGHT_CHANGED", "EXECUTION_WINDOW_EXPIRED"):
                        raise
                    receipts[key]["execution_error"] = exc.code
                    continue
                probe.collect(selected)
                if spec["scenario"] == "OPT-10":
                    push(t + 1, 2, "DUPLICATE", key)
                delay = 100 if selected in ("ATOMIC_PVP", "CORRESPONDENT") else 10
                push(t + delay, 2, "ADVANCE", key)
                continue
            key = payload
            receipt = receipts[key]
            r = receipt["selected"]
            world = worlds[r]
            if kind == "DUPLICATE":
                router.execute(run, key, t, prepare_only=True)
                probe.collect(r)
                continue
            state = _payment(store, world, key)
            if state["economic_status"] in ("REJECTED", "EXPIRED", "ABORTED"):
                continue
            if state.get("reason") in {
                "PARTICIPANT_DOWN",
                "SG_FAST_UNAVAILABLE",
                "PARTICIPANT_DOWN_PREPARED",
                "ONWARD_MESSAGE_LOST",
                "POSTED_ACK_MISSING",
                "INSUFFICIENT_CORRESPONDENT_LIQUIDITY",
                "FX_PROVIDER_INVENTORY_SHORTAGE",
            }:
                recovery_attempts += 1
            if r == "INSTANT_LINK" and any(
                m["ack_status"] == "LOST" for m in InstantLink(store).messages(world, key)
            ):
                recovery_attempts += 1
            if r == "INSTANT_LINK":
                stage = "SOURCE" if not state["source_final"] else "DESTINATION"
                parties = {"ATLAS_US", "STERLING_UK"} if stage == "SOURCE" else {"MERIDIAN_SG", "STERLING_UK"}
                lost = (
                    spec["scenario"] == "OPT-10" and stage == "DESTINATION" and not state["destination_final"]
                )
                InstantLink(store).deliver(
                    world, key, stage, t, available=not bool(parties & down), lose_ack=lost
                )
            else:
                router.recover(run, key, t)
            progress_attempts += 1
            probe.collect(r)
            state = _payment(store, world, key)
            terminal = state["economic_status"] in ("SETTLED", "REJECTED", "EXPIRED", "ABORTED")
            if terminal:
                if (
                    state["economic_status"] == "SETTLED"
                    and r == "INSTANT_LINK"
                    and spec["scenario"] == "OPT-10"
                    and any(m["ack_status"] == "LOST" for m in InstantLink(store).messages(world, key))
                ):
                    push(t + 10, 2, "ADVANCE", key)
                elif (
                    state["economic_status"] == "SETTLED"
                    and r == "CORRESPONDENT"
                    and state["ack_status"] != "ACKNOWLEDGED"
                ):
                    push(t + 10, 2, "ADVANCE", key)
                elif state["economic_status"] == "SETTLED" and r == "ATOMIC_PVP":
                    Ledger(store).acknowledge(world, key)
                    probe.collect(r)
                continue
            if r == "INSTANT_LINK":
                next_time = (
                    t + 100
                    if bool(
                        (
                            {"MERIDIAN_SG", "STERLING_UK"}
                            if state["source_final"]
                            else {"ATLAS_US", "STERLING_UK"}
                        )
                        & down
                    )
                    else max(
                        t + 1,
                        state["destination_due_ms"] if state["source_final"] else state["source_due_ms"],
                    )
                )
            elif r == "CORRESPONDENT":
                with store.transaction(world) as tx:
                    prior = (
                        tx.one(
                            "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=:i",
                            p=key,
                            i=state["next_stage"] - 1,
                        )
                        if state["next_stage"]
                        else None
                    )
                    hop = tx.one(
                        "SELECT data FROM cb_hops WHERE run_id=:r AND payment_id=:p AND hop_index=:i",
                        p=key,
                        i=state["next_stage"],
                    )
                next_time = (
                    t + 100
                    if state["economic_status"] == "PENDING_EXPOSURE"
                    or state.get("reason") == "INSUFFICIENT_CORRESPONDENT_LIQUIDITY"
                    else t + 10
                    if prior and prior["data"]["delivery_status"] != "ACKNOWLEDGED"
                    else max(t + 1, state["last_posted_ms"] + hop["data"]["delay_ms"])
                )
            else:
                next_time = t + 100
            push(next_time, 2, "ADVANCE", key)
    finally:
        event.remove(store.engine, "before_cursor_execute", probe.before)
        event.remove(store.engine, "after_cursor_execute", probe.after)
        _active_probe.reset(probe_token)
    audits = {r: reconcile(store, w) for r, w in worlds.items()}
    route_audit = reconcile(store, run)
    if any(a["financial_errors"] for a in [*audits.values(), route_audit]):
        raise RuntimeError("FINANCIAL_SAFETY_FAILURE")
    probe.integrate(spec["horizon_ms"])
    outcomes = {}
    beneficiary_totals: dict[str, int] = {}
    latencies = []
    completed = set()
    rejected = failed = pending = 0
    for key, receipt in receipts.items():
        r = receipt["selected"]
        state = _payment(store, worlds[r], key) if r and not receipt["execution_error"] else None
        outcomes[key] = {"receipt": receipt, "payment": state}
        if state and state["economic_status"] == "SETTLED":
            completed.add(key)
            currency = state["command"]["destination_currency"]
            amount = state["beneficiary_minor"] if r == "CORRESPONDENT" else state["quote"]["output_minor"]
            beneficiary_totals[currency] = beneficiary_totals.get(currency, 0) + amount
            latencies.append(
                (state.get("settled_ms", state.get("destination_final_ms"))) - receipt["arrival_ms"]
            )
        elif not state or state["economic_status"] == "REJECTED":
            rejected += 1
        elif state["economic_status"] in ("EXPIRED", "ABORTED"):
            failed += 1
        else:
            pending += 1
    fees: dict[str, int] = {}
    completed_fees: dict[str, int] = {}
    drawdowns = []
    obligations = []
    duplicate_groups = 0
    remaining = []
    for r, projection in probe.projections.items():
        for entry in projection["journals"].values():
            if not entry["payment_id"]:
                continue
            for line in entry["lines"]:
                a = line["account_id"]
                currency = entry["currency"]
                if a.startswith("OPERATOR:") or (a.startswith("GL:") and ":FEES:" in a):
                    fees[currency] = fees.get(currency, 0) + line["delta"]
                    if entry["payment_id"] in completed:
                        completed_fees[currency] = completed_fees.get(currency, 0) + line["delta"]
        for a, pos in projection["positions"].items():
            if projection["accounts"][a]["kind"] != "CLAIM" or a.startswith(("TRANSIT:", "BENEFICIARY:")):
                continue
            currency = projection["accounts"][a]["currency"]
            opening = initial[r]["positions"].get(a, {"posted": 0})["posted"]
            remaining.append(
                {
                    "rail": r,
                    "account": a,
                    "owner": projection["accounts"][a]["owner"],
                    "issuer": projection["accounts"][a]["issuer"],
                    "currency": currency,
                    **pos,
                    "available_minor": pos["posted"] - pos["reserved"] - pos["buffer"],
                }
            )
            if opening > pos["posted"]:
                drawdowns.append(
                    {
                        "rail": r,
                        "account": a,
                        "currency": currency,
                        "posted_drawdown_minor": opening - pos["posted"],
                    }
                )
        for p in projection["payments"].values():
            if p.get("obligation"):
                obligations.append({"rail": r, **p["obligation"]})
        if "correspondent" in projection:
            obligations.extend(
                {"rail": r, **o}
                for o in projection["correspondent"]["obligations"].values()
                if o["status"] == "OPEN"
            )
        if r in ("ATOMIC_PVP", "INSTANT_LINK"):
            for key, p in projection["payments"].items():
                entries = [j for j in projection["journals"].values() if j["payment_id"] == key]
                if len(entries) > 2:
                    duplicate_groups += len(entries) - 2
        else:
            for h in projection.get("correspondent", {}).get("hops", {}).values():
                if len(h["journal_ids"]) > 2:
                    duplicate_groups += len(h["journal_ids"]) - 2
    if duplicate_groups:
        raise RuntimeError("DUPLICATE_SETTLEMENT_FAILURE")
    latencies.sort()

    def percentile(p):
        return latencies[max(0, math.ceil(len(latencies) * p) - 1)] if latencies else None

    metrics = {
        "submitted": len(receipts),
        "eligible": sum(bool(r["selected"]) for r in receipts.values()),
        "completed": len(completed),
        "rejected": rejected,
        "safely_failed": failed,
        "pending": pending,
        "success_rate": {"numerator": len(completed), "denominator": len(receipts)},
        "fees_by_currency": fees,
        "completed_payment_cost_by_currency": {
            c: {"numerator": n, "denominator": len(completed)} for c, n in completed_fees.items()
        },
        "beneficiary_credited_by_currency": beneficiary_totals,
        "peak_obligations_by_currency": probe.obligation_peak,
        "obligation_amount_time_minor_ms": probe.obligation_area,
        "fx_cost": None,
        "funding_drawdown_by_account": drawdowns,
        "peak_reserved_by_currency": probe.peak,
        "peak_reserved_by_account": probe.reservation_peaks,
        "remaining_liquidity": remaining,
        "pending_obligations": obligations,
        "reconciliation_errors": 0,
        "duplicate_financial_postings": duplicate_groups,
        "latency_ms": {
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "sample_size": len(latencies),
        },
        "progress_attempts": progress_attempts,
        "recovery_attempts": recovery_attempts,
    }
    aliases: dict[str, str] = {w: r for r, w in worlds.items()}
    aliases[run] = "ROUTING"
    canonical = {
        "metrics": metrics,
        "outcomes": outcomes,
        "decisions": _normalize(decisions, aliases),
        "history_hashes": {r: a["replay"]["event_hash"] for r, a in audits.items()},
    }
    return {
        "policy": policy.model_dump(mode="json"),
        "physical_runs": {"control": run, **worlds},
        "input_hash": digest(spec),
        "workload_hash": digest(spec["workload"]),
        "configuration_hash": digest(
            {k: v for k, v in spec.items() if k not in ("workload", "faults", "seed")}
        ),
        "initial_state_hash": digest(initial),
        "fault_schedule_hash": digest(spec["faults"]),
        "metrics": metrics,
        "decisions": decisions,
        "outcomes": outcomes,
        "financial_audits": audits,
        "routing_audit": route_audit,
        "canonical_result_hash": digest(canonical),
        "timing": {
            "algorithm_ns": algo_ns,
            "decision_including_database_seconds": decision_db_seconds,
            "sql_statement_seconds": probe.sql_seconds,
            "sql_statement_count": probe.sql_count,
            "wall_seconds": time.perf_counter() - wall,
        },
        "freeze_observations": freeze_observations,
    }


def compare(store, scenario, seed=71, count=5, persist=True, include_v2=False):
    spec = specification(scenario, seed, count)
    results = []
    for version in (*VERSIONS, "LIQUIDITY_AWARE_V2") if include_v2 else VERSIONS:
        try:
            results.append(run_policy(store, spec, version))
        except Exception as exc:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            failure = {
                "status": "FAILED_STOP",
                "specification": spec,
                "policy": version,
                "completed_policy_runs": results,
                "error_type": type(exc).__name__,
                "reason": str(exc)
                if isinstance(exc, (RuntimeError, AssertionError))
                else "Execution interrupted; native financial history preserved",
            }
            with (ARTIFACTS / ("failure-" + uuid.uuid4().hex + ".json")).open("x", encoding="utf-8") as f:
                json.dump(failure, f, indent=2)
            raise
    for field in (
        "input_hash",
        "workload_hash",
        "configuration_hash",
        "initial_state_hash",
        "fault_schedule_hash",
    ):
        assert len({r[field] for r in results}) == 1, field
    identifier = uuid.uuid4().hex
    result = {
        "id": identifier,
        "specification": spec,
        "policies": results,
        "fairness_checked": True,
        "interpretation": "Exploratory; see registered complete cohort, not a single-scenario superiority claim",
    }
    if persist:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        with (ARTIFACTS / (identifier + ".json")).open("x", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    return result
