"""One sequential, preregistered O1 iteration; unique worlds and checkpoint artifacts."""

import argparse
import ctypes
import hashlib
import json
import time
from collections import Counter
from ctypes import wintypes
from pathlib import Path

from experiments.p1c_o1_diagnose import detail
from railstorm.domain import digest
from railstorm.optimizer_experiments import _history, run_policy, specification
from railstorm.optimizer_v2 import diagnostic_mode
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
VERSIONS = ("fixed-order-v1", "LIQUIDITY_AWARE_V1", "LIQUIDITY_AWARE_V2")
FAMILIES = (
    "OPT-01",
    "OPT-02",
    "OPT-03",
    "OPT-04",
    "OPT-05",
    "OPT-07",
    "OPT-08",
    "OPT-09",
    "OPT-10",
    "FEE-01",
)


def spec_for(family, seed, count):
    spec = specification("OPT-01" if family == "FEE-01" else family, seed, count)
    spec["protocol"] = "optimizer-o1-v2-queued-backoff"
    spec["family"] = family
    if family == "FEE-01":
        for d in spec["workload"]:
            d["amount_minor"] -= 9000
    if family == "OPT-09":
        spec["faults"][0]["end"] = spec["horizon_ms"] + 300
    return spec


def memory():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("peak", "rss", "qpp", "qpc", "qnp", "qnc", "page", "peakpage")
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    get = ctypes.windll.psapi.GetProcessMemoryInfo
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not get(current(), ctypes.byref(value), value.cb):
        raise RuntimeError("RSS_MEASUREMENT_FAILED")
    return {"rss_bytes": value.rss, "peak_rss_bytes": value.peak}


def enrich(store, result, spec):
    histories = {r: _history(store, w) for r, w in result["physical_runs"].items() if r != "control"}
    result["per_payment"] = [detail(store, result, d, histories) for d in result["decisions"]]
    result["reservation_amount_time_minor_ms"] = {}
    for rail, events in histories.items():
        active = {}
        for event in events:
            p = event["payload"]
            if event["event_type"] == "RESERVATION_CHANGED":
                if p["status"] == "ACTIVE":
                    active[p["id"]] = (event["logical_time_ms"], p)
                elif p["id"] in active:
                    at, hold = active.pop(p["id"])
                    account = rail + "/" + hold["account_id"]
                    totals = result["reservation_amount_time_minor_ms"]
                    totals[account] = totals.get(account, 0) + hold["amount"] * (
                        event["logical_time_ms"] - at
                    )
        for at, hold in active.values():
            account = rail + "/" + hold["account_id"]
            totals = result["reservation_amount_time_minor_ms"]
            totals[account] = totals.get(account, 0) + hold["amount"] * (spec["horizon_ms"] - at)
    result["route_distribution"] = dict(Counter(d["selected"] or "NO_ROUTE" for d in result["decisions"]))
    assert result["metrics"]["submitted"] == spec["count"], "All assigned intents must enter denominator"
    assert all(a["replay"]["match"] for a in result["financial_audits"].values())
    assert result["routing_audit"]["replay"]["match"]


def run_group(store, plan):
    results = []
    spec = plan["specification"]
    modes = (
        (("LIQUIDITY_AWARE_V2", m) for m in ("cost_only", "exposure_only", "full"))
        if plan["stage"] == "ablation"
        else ((v, "full") for v in VERSIONS)
    )
    for version, mode in modes:
        token = diagnostic_mode.set(mode)
        try:
            result = run_policy(store, spec, version)
        finally:
            diagnostic_mode.reset(token)
        result["diagnostic_mode"] = mode
        enrich(store, result, spec)
        results.append(result)
        print(
            version,
            mode,
            {k: result["metrics"][k] for k in ("completed", "pending", "fees_by_currency")},
            flush=True,
        )
    for name in (
        "input_hash",
        "workload_hash",
        "initial_state_hash",
        "configuration_hash",
        "fault_schedule_hash",
    ):
        assert len({r[name] for r in results}) == 1, "UNEQUAL_" + name
    return {"plan": plan, "fairness_checked": True, "policies": results}


def plans():
    entries = [("debug", "OPT-01", 11, 5), ("integration", "OPT-07", 71, 25)]
    entries += [("primary", family, (1021, 2039, 4093)[i % 3], 100) for i, family in enumerate(FAMILIES)]
    entries += [("ablation", family, 11, 5) for family in ("OPT-01", "OPT-02", "OPT-09", "FEE-01")]
    return [
        {
            "stage": stage,
            "family": family,
            "seed": seed,
            "count": count,
            "specification": spec_for(family, seed, count),
        }
        for stage, family, seed, count in entries
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("debug", "integration", "primary", "ablation", "freeze"))
    args = parser.parse_args()
    protocol = {
        "version": "o1-one-v2-iteration-corrected-scheduler",
        "planned": plans(),
        "primary": "Completed beneficiary payments / all assigned valid intents by horizon",
        "seed_assignment": "1021,2039,4093 cycle across ten families; prior131,257,509 cohort interrupted after queued1ms retry instrumentation defect; no policy tuning",
        "no_tuning": "One untuned rule; no changes after primary source freeze; stop after this evaluation",
        "large_workload": "100 intents per primary family; 500 and 10000 not scheduled",
        "execution": "Independent scenario blocks concurrent; policies within each block sequential. Queued insufficient correspondent liquidity retries at100ms, identical to pending exposure backoff. V2 policy source unchanged from first freeze.",
        "limitations": "Fixed FX, no FX spread valuation, independent rail funding, deterministic fixtures",
        "gate_a_sha256": hashlib.sha256((DOCS / "P1C_O1_diagnosis.json").read_bytes()).hexdigest(),
    }
    path = DOCS / "P1C_O1_plan_v2.json"
    if path.exists():
        assert json.loads(path.read_text()) == protocol, "PLAN_CHANGED"
    else:
        path.write_text(json.dumps(protocol, indent=2))
    sources = [
        *sorted((ROOT / "railstorm").rglob("*.py")),
        Path(__file__),
        ROOT / "experiments/p1c_o1_diagnose.py",
    ]
    frozen = {
        "plan_hash": digest(protocol),
        "source_sha256": {
            str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        },
    }
    freeze_path = DOCS / "P1C_O1_frozen_v2.json"
    if args.stage in ("freeze", "primary", "ablation"):
        if freeze_path.exists():
            assert json.loads(freeze_path.read_text()) == frozen, "FROZEN_SOURCE_CHANGED"
        else:
            assert args.stage == "freeze", "FREEZE_BEFORE_EVALUATION"
            freeze_path.write_text(json.dumps(frozen, indent=2))
        if args.stage == "freeze":
            print("Frozen", digest(frozen))
            return
    store = Store()
    target = DOCS / f"P1C_O1_{args.stage}_v2_results.json"
    output = (
        json.loads(target.read_text()) if target.exists() else {"plan_hash": digest(protocol), "runs": []}
    )
    assert output["plan_hash"] == digest(protocol)
    selected = [p for p in plans() if p["stage"] == args.stage]
    started = time.perf_counter()
    try:
        for index, entry in enumerate(selected):
            if index < len(output["runs"]):
                continue
            print(
                f"Starting {args.stage} {index + 1}/{len(selected)} {entry['family']} seed={entry['seed']} N={entry['count']}",
                flush=True,
            )
            before = memory()
            group = run_group(store, entry)
            group["resource"] = {
                "before": before,
                "after": memory(),
                "scope": "Windows Python client only; database excluded",
            }
            output["runs"].append(group)
            output["status"] = "IN_PROGRESS"
            target.write_text(json.dumps(output, indent=2))
    except BaseException:
        output["status"] = "FAILED_STOP"
        target.write_text(json.dumps(output, indent=2))
        raise
    output["status"] = "COMPLETE"
    output["invocation_wall_seconds"] = time.perf_counter() - started
    target.write_text(json.dumps(output, indent=2))
    print("Complete", args.stage, output["invocation_wall_seconds"], flush=True)


if __name__ == "__main__":
    main()
