"""Selected release identity and isolated quote access-path evidence; no credentials."""

import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import text

from experiments.r4_profile import resources
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"


def main():
    baseline = json.loads((OUT / "baseline.json").read_text())
    source = Store()
    isolated = Store(
        source.engine.url.set(database=baseline["database"]).render_as_string(hide_password=False)
    )
    run = json.loads((OUT / "final-1000-1-modified.json").read_text())["run_id"]
    plans = {}
    with isolated.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT current_database()").scalar_one() == baseline["database"]
        for label in ("indexed", "original_access_path"):
            if label == "original_access_path":
                conn.exec_driver_sql("DROP INDEX market_quote_lookup")
            plans[label] = conn.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT payload FROM events WHERE run_id=:r "
                    "AND event_type='MARKET_QUOTE' AND payload->>'pair'=:pair ORDER BY seq DESC LIMIT 1"
                ),
                {"r": run, "pair": "USD/SGD"},
            ).scalar_one()
        conn.exec_driver_sql(
            "CREATE INDEX market_quote_lookup ON events(run_id,(payload->>'pair'),seq DESC) WHERE event_type='MARKET_QUOTE'"
        )
        head = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        size = conn.exec_driver_sql("SELECT pg_database_size(current_database())").scalar_one()
        assert head == "008"
    (OUT / "quote_access_plans.json").write_text(json.dumps(plans, indent=2))
    with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R5_REVIEW.zip") as zipped:
        manifest = json.loads(zipped.read("railstorm/SHA256_MANIFEST.json"))
        checked = [
            name
            for name in manifest
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
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == manifest[name] for name in checked
        )
    validation = ROOT / ".work/r6/validation"
    checked_validation = []
    for folder in ("railstorm", "tests", "migrations", "scenarios"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(ROOT)
                assert (validation / relative).read_bytes() == path.read_bytes(), str(relative)
                checked_validation.append(relative.as_posix())
    report = {
        "python": sys.version,
        "os": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("SQLAlchemy", "psycopg", "alembic", "pytest", "ruff", "mypy")
        },
        "database": baseline["database"],
        "database_bytes_after_campaign": size,
        "migration_head": head,
        "configuration_and_frontend_unchanged_from_R5": checked,
        "validation_source_matches_release": checked_validation,
        "resources_final": resources(),
        "native_server_state": "Existing workspace synthetic server restarted; prior databases and R6 database retained",
        "container_state": "Own R6 stack stopped; named volumes retained",
    }
    (OUT / "environment.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "migration": head,
                "validation_source_files": len(checked_validation),
                "config_frontend_checks": len(checked),
                "indexed_quote_ms": plans["indexed"][0]["Execution Time"],
                "original_quote_scan_ms": plans["original_access_path"][0]["Execution Time"],
            }
        )
    )
    isolated.engine.dispose()
    source.engine.dispose()


if __name__ == "__main__":
    main()
