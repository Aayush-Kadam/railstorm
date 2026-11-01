"""Freeze original benchmark contracts and profile one bounded mixed-rail run."""

import cProfile
import ctypes
import hashlib
import json
import pstats
import shutil
from pathlib import Path

from railstorm.optimizer_experiments import run_policy, specification
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r4_evidence"


def resources():
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in (
                "total",
                "available",
                "page_total",
                "page_available",
                "virtual_total",
                "virtual_available",
                "extended",
            )
        ]

    m = Memory()
    m.length = ctypes.sizeof(m)
    assert ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"ram_total": m.total, "ram_available": m.available, "disk_free": shutil.disk_usage(ROOT).free}


def main():
    OUT.mkdir(exist_ok=True)
    source = ROOT / "railstorm/audit.py"
    frozen = OUT / "baseline_audit.py"
    if frozen.exists():
        raise FileExistsError("Do not overwrite frozen baseline")
    frozen.write_bytes(source.read_bytes())
    historical = json.loads((ROOT / "scenarios/stress_10000.json").read_text())
    mixed = specification("OPT-07", seed=20261029, count=25)
    manifest = {
        "historical_atomic": historical,
        "secondary_mixed": mixed,
        "baseline_audit_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "contract": "Historical atomic synthetic stress; execution includes create_run and batched step calls, excludes retrospective audit/replay. Secondary fixed-order OPT07 separate isolated worlds includes routing mandatory audits; not the historical acceptance workload.",
        "resources": resources(),
        "bounded_budget": "No single execution tier >60 seconds unless explicitly justified after smaller tiers; no automatic10000 escalation.",
    }
    (OUT / "contract.json").write_text(json.dumps(manifest, indent=2))
    profiler = cProfile.Profile()
    result = profiler.runcall(run_policy, Store(), mixed, "fixed-order-v1")
    functions = []
    for (file, line, name), (primitive, calls, self_s, inclusive_s, callers) in getattr(
        pstats.Stats(profiler), "stats"
    ).items():
        functions.append(
            {
                "file": Path(file).name,
                "name": name,
                "calls": calls,
                "self_seconds": self_s,
                "inclusive_seconds": inclusive_s,
            }
        )
    report = {
        "profiled_run": result,
        "top_self": sorted(functions, key=lambda r: r["self_seconds"], reverse=True)[:35],
        "audit_components": [
            r
            for r in functions
            if r["name"] in ("state_digest", "_replay_check", "reconcile", "iterencode", "snapshot")
        ],
        "resources_after": resources(),
    }
    (OUT / "baseline_profile.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"timing": result["timing"], "audit_components": report["audit_components"]}))


if __name__ == "__main__":
    main()
