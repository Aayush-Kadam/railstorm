"""Repeatable correspondent matrix, golden books and bounded-read memory evidence."""

import ctypes
import json
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from railstorm.audit import reconcile, replay_check
from railstorm.cb_simulation import CorrespondentScenario, chain_inspection, create_correspondent_run
from railstorm.domain import canonical, digest
from railstorm.simulation import metrics, step
from railstorm.store import Store


def memory():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in ("peak", "rss", "qpp", "qpc", "qnp", "qnc", "page", "peakpage")
        ]

    value = Counters()
    value.cb = ctypes.sizeof(value)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    get = ctypes.windll.psapi.GetProcessMemoryInfo
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not get(current(), ctypes.byref(value), value.cb):
        raise RuntimeError("RSS measurement failed")
    return {"rss_bytes": value.rss, "peak_rss_bytes": value.peak}


store = Store()
folder = Path("scenarios/correspondent")
out = Path("experiments/reports/p1a")
out.mkdir(parents=True, exist_ok=True)
expected = json.loads((folder / "expectations.json").read_text())
matrix = []
for path in sorted(folder.glob("CB-*.json")):
    scenario = CorrespondentScenario.model_validate_json(path.read_text())
    run = "p1a-evidence-" + scenario.scenario_id + "-" + uuid.uuid4().hex[:10]
    with store.engine.begin() as conn:
        from sqlalchemy import text

        config = scenario.model_dump(mode="json")
        conn.execute(
            text(
                "INSERT INTO scenarios(id,fingerprint,config) VALUES(:id,:f,CAST(:c AS jsonb)) ON CONFLICT(id) DO NOTHING"
            ),
            {"id": scenario.scenario_id, "f": digest(config), "c": canonical(config)},
        )
    create_correspondent_run(store, run, scenario)
    while not step(store, run, 50)["done"]:
        pass
    verification = reconcile(store, run)
    replay = replay_check(store, run)
    with store.transaction(run) as tx:
        chain = chain_inspection(tx, "p000000")
        books = tx.rows(
            "SELECT a.id,a.issuer,a.currency,a.kind,p.posted,p.reserved FROM accounts a JOIN positions p ON a.run_id=p.run_id AND a.id=p.account_id WHERE a.run_id=:r AND p.posted<>0 ORDER BY a.issuer,a.id"
        )
    e = expected[scenario.scenario_id]
    m = metrics(store, run)
    assert verification["financial_errors"] == 0 and replay["match"]
    assert chain["payment"]["economic_status"] == e["economic_status"]
    assert chain["pending_exposure_minor"] == e["open_exposure_minor"]
    assert {c: v for c, v in m["fees_by_currency_minor"].items() if v} == e["fees_minor"]
    item = {
        "scenario": scenario.scenario_id,
        "run_id": run,
        "expected": e,
        "metrics": m,
        "chain": chain,
        "verification": verification,
        "replay": replay,
        "books": books,
    }
    (out / (scenario.scenario_id + ".json")).write_text(json.dumps(item, indent=2), encoding="utf-8")
    matrix.append(
        {
            "scenario": scenario.scenario_id,
            "status": chain["payment"]["economic_status"],
            "exposure": chain["pending_exposure_minor"],
            "financial_errors": 0,
            "replay_match": True,
            "state_hash": replay["state_hash"],
        }
    )
(out / "matrix.json").write_text(json.dumps(matrix, indent=2), encoding="utf-8")

run = "p1a-memory-" + uuid.uuid4().hex[:10]
scenario = CorrespondentScenario(scenario_id="p1a-memory", payment_count=100)
before = memory()
start = time.perf_counter()
create_correspondent_run(store, run, scenario)
while not step(store, run, 100)["done"]:
    pass
execution = time.perf_counter() - start
after_execution = memory()
start = time.perf_counter()
with store.transaction(run) as tx:
    detail = chain_inspection(tx, "p000000")
inspection_seconds = time.perf_counter() - start
after_inspection = memory()
start = time.perf_counter()
audit = reconcile(store, run)
audit_seconds = time.perf_counter() - start
after_audit = memory()
assert audit["financial_errors"] == 0 and audit["replay"]["match"]
report = {
    "run_id": run,
    "payment_count": 100,
    "before": before,
    "execution_seconds": execution,
    "after_execution": after_execution,
    "inspection_seconds": inspection_seconds,
    "after_inspection": after_inspection,
    "audit_seconds": audit_seconds,
    "after_audit": after_audit,
    "inspection_returned_events": len(detail["events"]),
    "inspection_event_limit": 500,
    "verification": audit,
    "metrics": metrics(store, run),
    "limitations": "Windows client RSS, same process after matrix; not isolated audit allocation. Routine chain events limited to 500 and continued by cursor; full audit materializes entire run history.",
}
(out / "memory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        {
            "matrix_cases": len(matrix),
            "memory": {k: v for k, v in report.items() if k not in {"verification", "metrics"}},
        }
    )
)
