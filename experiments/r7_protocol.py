"""Small libpq protocol diagnostic, excluded from every performance median."""

import argparse
import json
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--implementation", choices=("baseline", "modified"), required=True)
args = parser.parse_args()
if args.implementation == "baseline":
    sys.path.insert(0, str(ROOT / ".work/r7/baseline"))

from sqlalchemy import event  # noqa: E402

from railstorm.audit import reconcile, replay_check  # noqa: E402
from railstorm.p2a_oracle import independent_audit  # noqa: E402
from railstorm.scenarios import Scenario  # noqa: E402
from railstorm.simulation import create_run, step  # noqa: E402
from railstorm.store import Store  # noqa: E402


def main():
    folder = ROOT / ".work/r7/traces"
    folder.mkdir(exist_ok=True)
    path = folder / (args.implementation + ".trace")
    assert not path.exists()
    store = Store()
    # Trace after authentication; no credentials/startup packets are captured.
    with store.engine.connect():
        pass
    connections = {}
    run = "r7-protocol-" + uuid.uuid4().hex
    with path.open("wb") as stream:

        def checkout(driver, record, proxy):
            if id(driver) not in connections:
                driver.pgconn.trace(stream.fileno())
                connections[id(driver)] = driver

        event.listen(store.engine.pool, "checkout", checkout)
        try:
            create_run(
                store,
                run,
                Scenario(scenario_id="r7-protocol", payment_count=2, principal_minor=10000, fee_minor=20),
            )
            while not step(store, run, 1000)["done"]:
                pass
        finally:
            for driver in connections.values():
                driver.pgconn.untrace()
            event.remove(store.engine.pool, "checkout", checkout)
    content = path.read_text(encoding="utf-8", errors="replace")
    counts = Counter(
        direction + ":" + message
        for direction, message in re.findall(r"\t([FB])\t\d+\t([^\t\r\n]+)", content)
    )
    assert counts and counts["F:Sync"] > 0
    native, replay, independent = (
        reconcile(store, run),
        replay_check(store, run),
        independent_audit(store, run),
    )
    assert native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    result = {
        "implementation": args.implementation,
        "sample_payments": 2,
        "message_counts": dict(counts),
        "trace_bytes": path.stat().st_size,
        "native": native,
        "replay": replay,
        "independent": independent,
        "scope": "libpq trace only2 intents, not a timing benchmark. F:Sync/F:Flush count actual protocol boundaries, not TCP packet RTTs. Auth excluded; raw payload trace retained locally only; review contains counts and integrity results.",
    }
    (ROOT / ("docs/r7_evidence/protocol-" + args.implementation + ".json")).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "implementation": args.implementation,
                "sync_messages": counts["F:Sync"],
                "flush_messages": counts["F:Flush"],
            }
        )
    )
    store.engine.dispose()


if __name__ == "__main__":
    main()
