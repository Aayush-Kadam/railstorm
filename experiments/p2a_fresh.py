"""Fresh migrated database smoke with native P2-A crisis audit."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from experiments.p2a_campaign import execute
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = Store()
    name = "railstorm_p2a_fresh_" + uuid.uuid4().hex[:8]
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = source.engine.url.set(database=name).render_as_string(hide_password=False)
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "RAILSTORM_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )
    fresh = Store(url)
    crisis = execute(fresh, "CH-04", seed=20261030, count=1)
    with fresh.engine.connect() as conn:
        from sqlalchemy import text

        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert head == "007" and crisis["status"] == "PASS"
    (ROOT / "docs/P2_A_fresh_database.json").write_text(
        json.dumps(
            {
                "database": name,
                "migration_head": head,
                "migration_output": migration.stdout + migration.stderr,
                "scenario": crisis["manifest"],
                "independent_audits": {r: a["independent"] for r, a in crisis["audit"].items()},
                "replay": {r: a["replay"] for r, a in crisis["audit"].items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Fresh migrations001-007 and CH-04 financial/replay audit passed")


if __name__ == "__main__":
    main()
