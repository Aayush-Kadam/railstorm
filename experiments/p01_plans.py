"""Read-only PostgreSQL plans for hot lookup paths; JSON contains no credentials."""

import json
from pathlib import Path

from sqlalchemy import text

from railstorm.store import Store

store = Store()
queries = {
    "payment": "SELECT * FROM payments WHERE run_id=:r AND id='p000050'",
    "position": "SELECT * FROM positions WHERE run_id=:r AND account_id='ATLAS_US:USD'",
    "decision": "SELECT decision FROM decisions WHERE run_id=:r AND payment_id='p000050'",
    "event_sequence": "SELECT * FROM events WHERE run_id=:r AND seq>100 ORDER BY seq LIMIT 100",
    "aggregate_version": "SELECT count(*) FROM events WHERE run_id=:r AND aggregate_id='p000050'",
    "reservation": "SELECT * FROM reservations WHERE run_id=:r AND payment_id='p000050' AND status='ACTIVE' ORDER BY id",
}
with store.engine.connect() as conn:
    plans = {
        name: conn.execute(
            text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query), {"r": "p01-baseline-verified-1000"}
        ).scalar()
        for name, query in queries.items()
    }
Path("experiments/reports/p01/query-plans.json").write_text(json.dumps(plans, indent=2))
print("Saved read-only hot-query plans")
