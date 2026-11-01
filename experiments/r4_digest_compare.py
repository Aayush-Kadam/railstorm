"""Isolate original/new canonical hash CPU on identical authoritative snapshots."""

import json
import statistics
import time

import railstorm.audit as audit
from experiments.r4_benchmark import OUT, baseline
from railstorm.store import Store


def main():
    old = baseline()
    store = Store()
    worlds = json.loads((OUT / "baseline_profile.json").read_text())["profiled_run"]["physical_runs"]
    results = {}
    for name, run in worlds.items():
        with store.transaction(run) as tx:
            state = audit.snapshot(tx)
        assert old.state_digest(state) == audit.state_digest(state)
        measurements: dict[str, list[float]] = {"baseline": [], "modified": []}
        for label in ("baseline", "modified", "modified", "baseline", "baseline", "modified"):
            function = old.state_digest if label == "baseline" else audit.state_digest
            start = time.perf_counter()
            for _ in range(20):
                observed = function(state)
            measurements[label].append((time.perf_counter() - start) / 20)
        results[name] = {
            "per_hash_seconds": measurements,
            "median_seconds": {k: statistics.median(v) for k, v in measurements.items()},
            "identical_hash": observed,
        }
    (OUT / "digest_compare.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({name: r["median_seconds"] for name, r in results.items()}))


if __name__ == "__main__":
    main()
