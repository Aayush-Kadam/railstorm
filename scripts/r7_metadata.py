"""Selected R7 environment, source, and supported PostgreSQL plan evidence."""

import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from zipfile import ZipFile

from psycopg import pq
from sqlalchemy import text

from experiments.r4_profile import resources
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"


def main():
    context = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    for prior in context["prior_archives"]:
        assert hashlib.sha256((ROOT.parent / prior["name"]).read_bytes()).hexdigest() == prior["sha256"]
    with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R6_REVIEW.zip") as zipped:
        original = json.loads(zipped.read("railstorm/SHA256_MANIFEST.json"))
    unchanged = [
        name
        for name in original
        if name.startswith(("frontend/src/", "migrations/"))
        or name
        in (
            "Dockerfile",
            "docker-compose.yml",
            ".dockerignore",
            "pyproject.toml",
            "uv.lock",
            "frontend/package.json",
            "frontend/pnpm-lock.yaml",
        )
    ]
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == original[name] for name in unchanged)
    different = [
        name
        for name in original
        if name.startswith("railstorm/")
        and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != original[name]
    ]
    assert set(different) == {"railstorm/store.py", "railstorm/market_quotes.py"}
    validation = ROOT / ".work/r7/validation-final"
    validated = []
    for folder in ("railstorm", "tests", "scenarios", "migrations"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                relative_file = path.relative_to(ROOT)
                assert (validation / relative_file).read_bytes() == path.read_bytes(), str(relative_file)
                validated.append(relative_file.as_posix())
    source = Store()
    isolated = Store(
        source.engine.url.set(database=context["database"]).render_as_string(hide_password=False)
    )
    run = json.loads((OUT / "stable1-1000-1-modified.json").read_text(encoding="utf-8"))["run_id"]
    with isolated.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT current_database()").scalar_one() == context["database"]
        head = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        assert head == "008"
        plans = {}
        for name, query, params in (
            (
                "position",
                "SELECT * FROM positions WHERE run_id=:r AND account_id=:a",
                {"r": run, "a": "ATLAS_US:USD"},
            ),
            (
                "market",
                "SELECT payload FROM events WHERE run_id=:r AND event_type='MARKET_QUOTE' AND payload->>'pair'=:p ORDER BY seq DESC LIMIT 1",
                {"r": run, "p": "USD/SGD"},
            ),
            (
                "reservations",
                "SELECT * FROM reservations WHERE run_id=:r AND payment_id=:p AND status='ACTIVE' ORDER BY id",
                {"r": run, "p": "p000001"},
            ),
        ):
            plans[name] = conn.execute(
                text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query), params
            ).scalar_one()
        size = conn.exec_driver_sql("SELECT pg_database_size(current_database())").scalar_one()
    (OUT / "read_query_plans.json").write_text(json.dumps(plans, indent=2), encoding="utf-8")
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "libpq": pq.version(),
        "pq_implementation": pq.__impl__,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("SQLAlchemy", "psycopg", "alembic", "pytest", "ruff", "mypy")
        },
        "migration_head": head,
        "database": context["database"],
        "database_bytes": size,
        "existing_runtime_files_changed": different,
        "new_runtime_file": "railstorm/native_sql.py",
        "unchanged_configuration_frontend_and_migrations": unchanged,
        "validation_source_matches_release": validated,
        "prior_archives_verified": len(context["prior_archives"]),
        "resources_final": resources(),
        "services": "Shared workspace server restarted for campaign, left available. Private R7 restart server stopped, data retained. Docker/WSL unavailable and not started.",
    }
    (OUT / "environment.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "migration": head,
                "validated_files": len(validated),
                "prior_archives": len(context["prior_archives"]),
            }
        )
    )
    isolated.engine.dispose()
    source.engine.dispose()


if __name__ == "__main__":
    main()
