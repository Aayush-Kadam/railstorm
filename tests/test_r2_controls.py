"""Real disposable-world corruption and duplicate-attempt negative controls."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from railstorm.audit import reconcile, replay_check
from railstorm.domain import PaymentCommand
from railstorm.p2a_oracle import independent_audit
from railstorm.rails.instant_link import InstantLink


@pytest.mark.parametrize("kind", ["projection", "false_finality", "event", "duplicate"])
def test_native_negative_control(store, kind):
    rail = InstantLink(store)
    run = "r2-control-" + uuid.uuid4().hex
    rail.initialize(run)
    rail.prepare(
        run, PaymentCommand(idempotency_key="p", principal_minor=10000, requested_rail="INSTANT_LINK")
    )
    rail.deliver(run, "p", "SOURCE", 10)
    opening = {
        "inspection": rail.inspect(run, "p"),
        "independent": independent_audit(store, run),
        "replay": replay_check(store, run),
    }
    assert opening["independent"]["pass"] and opening["replay"]["match"]
    with store.engine.connect() as conn:
        original_payment = conn.execute(
            text("SELECT data FROM payments WHERE run_id=:r AND id='p'"), {"r": run}
        ).scalar_one()
    if kind == "duplicate":
        with pytest.raises(IntegrityError):
            with store.transaction(run) as tx:
                tx.journal(
                    "p:us-final",
                    "p",
                    "USD",
                    [("ATLAS_US:USD", -10000), ("STERLING_UK:USD", 10000)],
                    issuer="US_FAST",
                )
    elif kind == "event":
        with pytest.raises(ProgrammingError, match="APPEND_ONLY"):
            with store.engine.begin() as conn:
                conn.execute(
                    text("UPDATE events SET event_type='TAMPERED' WHERE run_id=:r AND seq=1"), {"r": run}
                )
    else:
        sql = {
            "projection": "UPDATE positions SET posted=posted+1 WHERE run_id=:r AND account_id='ATLAS_US:USD'",
            "false_finality": "UPDATE payments SET data=jsonb_set(data,'{economic_status}','\"SETTLED\"') WHERE run_id=:r AND id='p'",
            "event": "UPDATE events SET event_type='TAMPERED' WHERE run_id=:r AND seq=1",
        }[kind]
        with store.engine.begin() as conn:
            conn.execute(text(sql), {"r": run})
    observed = {
        "independent": independent_audit(store, run),
        "native": reconcile(store, run),
        "replay": replay_check(store, run),
    }
    if kind in ("duplicate", "event"):
        assert observed["independent"]["pass"] and observed["replay"]["match"]
    else:
        assert observed["native"]["financial_errors"] > 0 and not observed["replay"]["match"]
        if kind != "event":
            assert not observed["independent"]["pass"]
    with store.engine.begin() as conn:
        if kind == "projection":
            conn.execute(
                text("UPDATE positions SET posted=posted-1 WHERE run_id=:r AND account_id='ATLAS_US:USD'"),
                {"r": run},
            )
        if kind == "false_finality":
            conn.execute(
                text("UPDATE payments SET data=CAST(:d AS jsonb) WHERE run_id=:r AND id='p'"),
                {"r": run, "d": json.dumps(original_payment)},
            )
    rail.recover(run, "p", 40)
    final = {
        "inspection": rail.inspect(run, "p"),
        "independent": independent_audit(store, run),
        "native": reconcile(store, run),
        "replay": replay_check(store, run),
    }
    assert (
        final["independent"]["pass"] and final["native"]["financial_errors"] == 0 and final["replay"]["match"]
    )
    assert len(final["inspection"]["journals"]) == 2
    out = Path(__file__).resolve().parents[1] / "docs/thunderbolt_r2_evidence"
    out.mkdir(exist_ok=True)
    (out / f"control-{kind}.json").write_text(
        json.dumps(
            {"kind": kind, "run_id": run, "opening": opening, "observed": observed, "restored_final": final},
            indent=2,
        )
    )
