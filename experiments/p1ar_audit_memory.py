"""Fresh-process full-coverage audit comparison on an existing synthetic run."""

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--run", required=True)
parser.add_argument("--label", required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.source.resolve()))
from railstorm.audit import reconcile  # noqa: E402
from railstorm.store import Store  # noqa: E402


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
        raise RuntimeError("RSS measurement failed")
    return {"rss_bytes": value.rss, "peak_rss_bytes": value.peak}


store = Store()
before = memory()
start = time.perf_counter()
audit = reconcile(store, args.run)
seconds = time.perf_counter() - start
after = memory()
assert audit["financial_errors"] == 0 and audit["replay"]["match"]
report = {
    "label": args.label,
    "source": str(args.source),
    "run": args.run,
    "before": before,
    "after": after,
    "reconcile_seconds": seconds,
    "audit": audit,
    "memory_scope": "Fresh Windows client process high-water; PostgreSQL server excluded; complete audit, no history cap.",
}
out = Path("experiments/reports/p1ar")
out.mkdir(parents=True, exist_ok=True)
(out / (args.label + ".json")).write_text(json.dumps(report, indent=2))
print(json.dumps({k: v for k, v in report.items() if k != "audit"}))
