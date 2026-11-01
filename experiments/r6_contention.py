"""Bounded independent-session shortage and duplicate-credit controls."""

import argparse
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--implementation", choices=("baseline", "modified"), required=True)
args = parser.parse_args()
if args.implementation == "baseline":
    sys.path.insert(0, str(ROOT / ".work/r6/baseline"))

from experiments.p2ar_contention import exercise  # noqa: E402
from railstorm.audit import reconcile, replay_check  # noqa: E402
from railstorm.domain import PaymentCommand  # noqa: E402
from railstorm.ledger import Ledger  # noqa: E402
from railstorm.p2a_oracle import independent_audit  # noqa: E402
from railstorm.store import Store  # noqa: E402


def main():
    store = Store()
    started = time.perf_counter()
    shortage = exercise(store)
    shortage_seconds = time.perf_counter() - started
    run = "r6-duplicate-" + uuid.uuid4().hex
    Ledger(store).initialize(run)

    def worker(_):
        independent = Store()
        started = time.perf_counter()
        try:
            result = Ledger(independent).submit(
                run, PaymentCommand(idempotency_key="same", principal_minor=10000)
            )
            return {"result": result, "seconds": time.perf_counter() - started}
        finally:
            independent.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (0, 1)))
    assert all(r["result"]["economic_status"] == "SETTLED" for r in results)
    with store.transaction(run) as tx:
        beneficiary = tx.position("MERIDIAN_SG:SGD")["posted"]
        groups = tx.one("SELECT count(*) n FROM journal_entries WHERE run_id=:r AND payment_id='same'")["n"]
    assert beneficiary == 100000000 + 13300 and groups == 2
    native, replay, independent = (
        reconcile(store, run),
        replay_check(store, run),
        independent_audit(store, run),
    )
    assert native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    report = {
        "implementation": args.implementation,
        "shortage": shortage,
        "shortage_seconds": shortage_seconds,
        "duplicate": {
            "run": run,
            "workers": results,
            "beneficiary": beneficiary,
            "journal_groups": groups,
            "native": native,
            "replay": replay,
            "independent": independent,
        },
    }
    (ROOT / ("docs/r6_evidence/contention-" + args.implementation + ".json")).write_text(
        json.dumps(report, indent=2)
    )
    print(
        json.dumps(
            {
                "implementation": args.implementation,
                "shortage": "PASS",
                "duplicate": "PASS",
                "max_observed_lock_waiters": max(shortage["observed_lock_waits"]),
            }
        )
    )


if __name__ == "__main__":
    main()
