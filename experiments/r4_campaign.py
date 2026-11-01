"""Sequential bounded final comparisons and atomic ladder with durable checkpoints."""

import json
import shutil
import statistics
import subprocess
import sys

from experiments.r4_benchmark import OUT
from experiments.r4_profile import resources


def run(arguments, timeout=120):
    result = subprocess.run(
        [sys.executable, "-m", "experiments.r4_benchmark", *arguments], timeout=timeout, check=True
    )
    assert result.returncode == 0


def main():
    if resources()["ram_available"] < 1024**3:
        raise RuntimeError("Insufficient available RAM for bounded campaign")
    run(["compare"], 180)
    rows = []
    for index, label in enumerate(("baseline", "modified", "modified", "baseline")):
        run(["atomic", "--count", "500", "--implementation", label])
        path = OUT / f"atomic-500-{label}.json"
        shutil.copyfile(path, OUT / f"atomic-500-{label}-repeat{index}.json")
        rows.append(json.loads(path.read_text()))
        (OUT / "matched_atomic_500.json").write_text(json.dumps(rows, indent=2))
    assert len({r["state_hash"] for r in rows}) == 1
    assert len({r["replay"]["event_hash"] for r in rows}) == 1
    assert len({r["workload_hash"] for r in rows}) == 1
    assert len({r["execution_sql"]["logical_executions"] for r in rows}) == 1
    print(
        json.dumps(
            {
                label: statistics.median(r["execution_seconds"] for r in rows if r["implementation"] == label)
                for label in ("baseline", "modified")
            }
        ),
        flush=True,
    )
    for count in (25, 100, 1000):
        if resources()["ram_available"] < 1024**3:
            raise RuntimeError("Resource gate stopped ladder")
        run(["atomic", "--count", str(count)])
    final = json.loads((OUT / "atomic-1000-modified.json").read_text())
    decision = {
        "target_10000_in_60": "UNVERIFIED",
        "attempted": False,
        "measured_1000_per_second": final["payments_per_second"],
        "required_per_second": 10000 / 60,
        "reason": "Measured1000 tier remains below required166.67/s; no plausible resource-bounded60-second10000 attempt justified. This is not a measured10000 failure or projected passing result.",
        "resources": resources(),
    }
    (OUT / "ladder_decision.json").write_text(json.dumps(decision, indent=2))


if __name__ == "__main__":
    main()
