"""Read-only database configuration and bounded event-page execution plan."""

import json
import platform
import sys

from sqlalchemy import text

from experiments.r4_profile import OUT, resources
from railstorm.store import Store


def main():
    store = Store()
    run = json.loads((OUT / "baseline_profile.json").read_text())["profiled_run"]["physical_runs"]["control"]
    with store.engine.connect() as conn:
        settings: dict[str, str] = dict(
            conn.execute(
                text(
                    "SELECT name,setting FROM pg_settings WHERE name IN ('server_version','fsync','synchronous_commit','full_page_writes')"
                )
            ).all()
        )
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        plan = conn.execute(
            text(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM events WHERE run_id=:r AND seq>0 ORDER BY seq LIMIT 512"
            ),
            {"r": run},
        ).scalar_one()
    result = {
        "python": sys.version.split()[0],
        "platform": platform.system(),
        "postgresql": settings,
        "migration_head": head,
        "database_scope": "existing local synthetic PostgreSQL database; same server and configuration for all comparisons",
        "database_size_bytes": size,
        "event_page_plan": plan,
        "resources": resources(),
    }
    (OUT / "environment.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "event_page_plan"}))


if __name__ == "__main__":
    main()
