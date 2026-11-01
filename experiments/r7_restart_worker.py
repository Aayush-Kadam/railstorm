"""Native three-rail durable state before/after a real isolated server restart."""

import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from railstorm.audit import reconcile, replay_check, snapshot
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.market_quotes import replace
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.correspondent import ChainCommand, CorrespondentRail
from railstorm.rails.instant_link import InstantLink
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "docs/r7_evidence/database_restart.json"


def capture(store, run):
    with store.transaction(run) as tx:
        state = snapshot(tx)
    native, replay, independent = (
        reconcile(store, run),
        replay_check(store, run),
        independent_audit(store, run),
    )
    assert native["financial_errors"] == 0 and replay["match"] and independent["pass"]
    return {"state": state, "native": native, "replay": replay, "independent": independent}


def server(store):
    with store.engine.connect() as conn:
        return {
            "started": str(conn.execute(text("SELECT pg_postmaster_start_time()")).scalar_one()),
            "fsync": conn.exec_driver_sql("SHOW fsync").scalar_one(),
            "synchronous_commit": conn.exec_driver_sql("SHOW synchronous_commit").scalar_one(),
        }


def main():
    store = Store()
    ledger, instant, correspondent = Ledger(store), InstantLink(store), CorrespondentRail(store)
    if sys.argv[1] == "before":
        assert not PATH.exists()
        runs = {
            name: "r7-restart-" + name + "-" + uuid.uuid4().hex
            for name in ("atomic", "instant", "correspondent")
        }
        atomic = runs["atomic"]
        ledger.initialize(atomic, {"fee_minor": 20})
        replace(
            store,
            atomic,
            pair="USD/SGD",
            version=1,
            numerator=133,
            denominator=100,
            at_ms=0,
            expires_ms=1000,
            fee_minor=20,
        )
        ledger.prepare(atomic, PaymentCommand(idempotency_key="p", principal_minor=10000))
        replace(
            store,
            atomic,
            pair="USD/SGD",
            version=2,
            numerator=140,
            denominator=100,
            at_ms=1,
            expires_ms=1000,
            fee_minor=20,
        )
        instant.initialize(runs["instant"])
        instant.prepare(
            runs["instant"],
            PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK"),
        )
        instant.deliver(runs["instant"], "p", "SOURCE", 10)
        correspondent.initialize(runs["correspondent"])
        correspondent.create(runs["correspondent"], ChainCommand(idempotency_key="p"))
        correspondent.advance(runs["correspondent"], "p", 100)
        before = {name: capture(store, run) for name, run in runs.items()}
        pending = before["instant"]["state"]["payments"]["p"]
        assert (
            pending["source_final"]
            and not pending["destination_final"]
            and pending["obligation"]["amount_minor"] == 13300
        )
        result = {
            "phase": "BEFORE_RESTART_PASS",
            "runs": runs,
            "server_before": server(store),
            "before": before,
        }
    else:
        result = json.loads(PATH.read_text(encoding="utf-8"))
        runs = result["runs"]
        result["server_after"] = server(store)
        assert result["server_after"]["started"] != result["server_before"]["started"]
        assert result["server_after"]["fsync"] == result["server_after"]["synchronous_commit"] == "on"
        persisted = {name: capture(store, run) for name, run in runs.items()}
        for name in runs:
            assert persisted[name]["state"] == result["before"][name]["state"]
            assert persisted[name]["replay"]["event_hash"] == result["before"][name]["replay"]["event_hash"]
        result["persisted_after_restart"] = persisted
        atomic = ledger.submit(runs["atomic"], PaymentCommand(idempotency_key="p", principal_minor=10000), 2)
        assert atomic["quote"]["version"] == 1 and atomic["quote"]["output_minor"] == 13300
        instant.recover(runs["instant"], "p", 40)
        for at in range(200, 1700, 100):
            correspondent.advance(runs["correspondent"], "p", at)
        result["after_recovery"] = {name: capture(store, run) for name, run in runs.items()}
        for name in runs:
            assert result["after_recovery"][name]["state"]["payments"]["p"]["economic_status"] == "SETTLED"
        for _ in range(2):
            ledger.submit(runs["atomic"], PaymentCommand(idempotency_key="p", principal_minor=10000), 3)
            instant.recover(runs["instant"], "p", 50)
            correspondent.advance(runs["correspondent"], "p", 1700)
        result["after_retry"] = {name: capture(store, run) for name, run in runs.items()}
        for name in runs:
            assert (
                result["after_retry"][name]["state"]["journals"]
                == result["after_recovery"][name]["state"]["journals"]
            )
            assert (
                result["after_retry"][name]["state"]["positions"]
                == result["after_recovery"][name]["state"]["positions"]
            )
        assert result["after_retry"]["instant"]["state"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
        result["phase"] = "RESTART_RECOVERY_RETRY_PASS"
    PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"phase": result["phase"], "rails": 3}), flush=True)
    store.engine.dispose()


if __name__ == "__main__":
    main()
