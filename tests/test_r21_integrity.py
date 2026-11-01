"""Copied evidence alteration is detected without disabling stored protections."""

import copy
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from railstorm.audit import replay_check
from railstorm.domain import PaymentCommand
from railstorm.event_integrity import verify_export
from railstorm.ledger import Ledger


def test_export_tamper_matrix(store):
    run = "r21-export-" + uuid.uuid4().hex
    ledger = Ledger(store)
    ledger.initialize(run)
    ledger.submit(run, PaymentCommand(idempotency_key="p", principal_minor=10000))
    rows: list[dict] = []
    while True:
        page = store.events(run, rows[-1]["seq"] if rows else 0, 512)
        if not page:
            break
        rows.extend(page)
    count, head = len(rows), rows[-1]["event_hash"]
    assert verify_export(rows, count, head)["match"]
    with pytest.raises(ProgrammingError, match="APPEND_ONLY"):
        with store.engine.begin() as conn:
            conn.execute(
                text("UPDATE events SET event_type='TAMPERED' WHERE run_id=:r AND seq=1"), {"r": run}
            )
    controls = {}
    for kind in ("financial", "deleted", "truncated", "finality"):
        altered = copy.deepcopy(rows)
        if kind == "financial":
            event = next(e for e in altered if e["event_type"] == "JOURNAL_POSTED")
            event["payload"]["lines"][0]["delta"] += 1
        elif kind == "deleted":
            del altered[len(altered) // 2]
        elif kind == "truncated":
            altered.pop()
        else:
            event = next(e for e in altered if e["event_type"] == "PAYMENT_CHANGED")
            event["payload"]["economic_status"] = "FORGED_FINALITY"
        controls[kind] = verify_export(altered, count, head)
        assert not controls[kind]["match"]
    assert store.events(run, 0, 512) == rows
    assert replay_check(store, run)["match"]
    out = Path(__file__).resolve().parents[1] / "docs/r21_evidence"
    out.mkdir(exist_ok=True)
    (out / "ET-01-06.json").write_text(
        json.dumps(
            {
                "events": rows,
                "expected_count": count,
                "expected_head": head,
                "controls": controls,
                "original_unchanged": True,
            },
            indent=2,
        )
    )
