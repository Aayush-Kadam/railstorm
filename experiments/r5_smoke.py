"""Bounded full-stack native payment and restart acceptance, without test mocks."""

import argparse
import json
import urllib.request
import uuid
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.ledger import Ledger
from railstorm.market_quotes import replace
from railstorm.p2a_oracle import independent_audit
from railstorm.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("initial", "restart"))
    parser.add_argument("--base", default="http://127.0.0.1:8840")
    parser.add_argument("--checkpoint", default="docs/r5_smoke.json")
    parser.add_argument("--restart-scope", choices=("backend", "stack"), default="backend")
    args = parser.parse_args()
    path = Path(args.checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = Store()

    def request(url, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(args.base + url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def create(mode):
        return request("/api/v1/live-crisis/sessions", {"scenario": mode, "seed": 20261031})

    def action(run, name):
        return request(f"/api/v1/live-crisis/sessions/{run}/actions", {"action": name})

    def verify(run):
        native = reconcile(store, run)
        independent = independent_audit(store, run)
        replay = replay_check(store, run)
        assert native["financial_errors"] == 0 and independent["pass"] and replay["match"]
        return {"native": native, "independent_worker_SQL_audit": independent, "replay": replay}

    assert request("/api/v1/health")["status"] == "OK"
    with urllib.request.urlopen(args.base + "/", timeout=10) as response:
        frontend = response.read().decode()
    assert '<div id="root">' in frontend
    with store.engine.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    expected_head = ScriptDirectory.from_config(
        Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    ).get_current_head()
    assert head == expected_head
    if args.phase == "initial":
        records = []
        for mode in ("atomic", "correspondent", "instant", "multicurrency", "expired", "shortage"):
            opening = create(mode)
            run = opening["session"]["id"]
            for _ in range(20):
                final = action(run, "ADVANCE")
                p = final["financial"]["payments"]["payment"]
                if p["economic_status"] in ("SETTLED", "REJECTED", "EXPIRED"):
                    break
            expected = "REJECTED" if mode == "shortage" else "EXPIRED" if mode == "expired" else "SETTLED"
            assert p["economic_status"] == expected
            if mode == "shortage":
                assert final["financial"]["positions"] == opening["financial"]["positions"]
            verified = verify(run)
            records.append({"scenario": mode, "opening": opening, "final": final, "checks": verified})
        lost = create("instant")
        lost_run = lost["session"]["id"]
        for name in ("ADVANCE", "LOSE_ACK", "ADVANCE", "ADVANCE", "RECOVER", "RETRY"):
            lost = action(lost_run, name)
        assert lost["financial"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
        assert len([j for j in lost["financial"]["journals"].values() if j["payment_id"] == "payment"]) == 2
        records.append({"scenario": "lost_ack_duplicate", "final": lost, "checks": verify(lost_run)})
        pending = create("instant")
        run = pending["session"]["id"]
        for name in ("ADVANCE", "OUTAGE", "ADVANCE", "ADVANCE"):
            pending = action(run, name)
        p = pending["financial"]["payments"]["payment"]
        assert p["source_final"] and not p["destination_final"] and p["obligation"]["amount_minor"] == 13300
        # Quote integrity representative: immutable native accepted quote survives newer market version.
        quote_run = "r5-quote-" + uuid.uuid4().hex
        ledger = Ledger(store)
        ledger.initialize(quote_run, {"fee_minor": 25})
        replace(
            store,
            quote_run,
            pair="USD/SGD",
            version=1,
            numerator=133,
            denominator=100,
            at_ms=0,
            expires_ms=100,
        )
        command = PaymentCommand(idempotency_key="q", principal_minor=10000)
        accepted = ledger.prepare(quote_run, command, 0)
        replace(
            store,
            quote_run,
            pair="USD/SGD",
            version=2,
            numerator=140,
            denominator=100,
            at_ms=1,
            expires_ms=100,
        )
        completed = ledger.submit(quote_run, command, 2)
        assert completed["quote"] == accepted["quote"] and completed["quote"]["version"] == 1
        quote_checks = verify(quote_run)
        result = {
            "phase": "INITIAL_PASS",
            "migration_head": head,
            "frontend_available": True,
            "records": records,
            "pending_before_restart": pending,
            "pending_checks": verify(run),
            "quote_checks": quote_checks,
            "quote": {"accepted": accepted, "completed": completed},
            "database_restart_verified": False,
        }
    else:
        result = json.loads(path.read_text())
        previous = result["pending_before_restart"]
        run = previous["session"]["id"]
        persisted = request(f"/api/v1/live-crisis/sessions/{run}")
        assert persisted["financial"] == previous["financial"]
        assert persisted["session"] == previous["session"]
        for name in ("RESTORE", "RECOVER", "RETRY", "AUDIT", "REPLAY"):
            final = action(run, name)
        assert final["financial"]["positions"]["MERIDIAN_SG:SGD"]["posted"] == 13300
        assert len([j for j in final["financial"]["journals"].values() if j["payment_id"] == "payment"]) == 2
        result.update(
            phase="RESTART_PASS",
            persisted_after_restart=persisted,
            final_after_restoration=final,
            final_checks=verify(run),
            application_restart_verified=True,
            restart_scope=args.restart_scope,
            database_restart_verified=args.restart_scope == "stack",
        )
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"phase": result["phase"], "migration_head": head, "financial_checks": "PASS", "frontend": "PASS"}
        )
    )


if __name__ == "__main__":
    main()
