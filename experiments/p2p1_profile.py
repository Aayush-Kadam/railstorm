"""Read-only bounded comprehensive audit profiling of preserved synthetic worlds."""

import cProfile
import json
import pstats
import time
from pathlib import Path

from railstorm.audit import reconcile
from railstorm.store import Store


def main():
    root = Path(__file__).resolve().parents[1]
    rows = []
    store = Store()
    for filename in ("P2_A_R_contention.json", "P2_A_R_ordering.json"):
        run = json.loads((root / "docs" / filename).read_text())["run_id"]
        profiler = cProfile.Profile()
        started = time.perf_counter()
        result = profiler.runcall(reconcile, store, run)
        elapsed = time.perf_counter() - started
        functions = []
        for key, value in getattr(pstats.Stats(profiler), "stats").items():
            file, line, name = key
            primitive, calls, self_seconds, inclusive_seconds, callers = value
            functions.append(
                {
                    "file": Path(file).name,
                    "function": name,
                    "calls": calls,
                    "self_seconds": self_seconds,
                    "inclusive_seconds": inclusive_seconds,
                }
            )
        rows.append(
            {
                "run_id": run,
                "elapsed_seconds": elapsed,
                "audit": result,
                "functions": sorted(functions, key=lambda x: x["self_seconds"], reverse=True)[:40],
            }
        )
    (root / "docs/P2_P1_initial_profile.json").write_text(json.dumps(rows, indent=2))
    print(json.dumps([{k: row[k] for k in ("run_id", "elapsed_seconds")} for row in rows]))


if __name__ == "__main__":
    main()
