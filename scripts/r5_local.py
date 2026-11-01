"""Provision and verify the new isolated local installation only."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r5_evidence"
FRESH = ROOT / ".work/r5/source"
PYTHON = FRESH / ".venv/Scripts/python.exe"
CONTEXT = json.loads((OUT / "context.json").read_text())
DATABASE = CONTEXT["database"]
assert re.fullmatch(r"railstorm_r5_[0-9a-f]{8}", DATABASE)
source = Store()
url = source.engine.url.set(database=DATABASE).render_as_string(hide_password=False)
ENV = {**os.environ, "RAILSTORM_DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"}
LOG = ROOT / ".work/r5/logs"


def command(label, args, timeout=120):
    with (LOG / (label + ".log")).open("w", encoding="utf-8") as output:
        result = subprocess.run(
            args, cwd=FRESH, env=ENV, stdout=output, stderr=subprocess.STDOUT, timeout=timeout
        )
    print(json.dumps({"command": label, "returncode": result.returncode}), flush=True)
    if result.returncode:
        print((LOG / (label + ".log")).read_text()[-3500:])
        raise SystemExit(result.returncode)


phase = sys.argv[1]
if phase == "provision":
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": DATABASE}).first()
        if exists:
            raise RuntimeError("Do not reuse a preconfigured installation database")
        conn.exec_driver_sql(f'CREATE DATABASE "{DATABASE}"')
    fresh = Store(url)
    with fresh.engine.connect() as conn:
        initial = conn.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ).scalar_one()
    assert initial == 0
    command("local_migration", [str(PYTHON), "-m", "alembic", "upgrade", "head"])
    command("local_fixtures", [str(PYTHON), "scripts/load_scenarios.py"])
    with fresh.engine.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        fixtures = conn.execute(text("SELECT count(*) FROM scenarios")).scalar_one()
    assert head == "007" and fixtures > 0
    (OUT / "local_database.json").write_text(
        json.dumps(
            {
                "database": DATABASE,
                "initial_public_table_count": initial,
                "migration_head": head,
                "loaded_fixtures": fixtures,
            },
            indent=2,
        )
    )
elif phase in ("initial", "restart"):
    command(
        "local_" + phase,
        [
            str(PYTHON),
            "-m",
            "experiments.r5_smoke",
            phase,
            "--base",
            "http://127.0.0.1:" + str(CONTEXT["local_port"]),
            "--checkpoint",
            "docs/r5_local_smoke.json",
        ],
    )
    shutil.copyfile(FRESH / "docs/r5_local_smoke.json", OUT / "local_smoke.json")
elif phase == "tests":
    command("local_pytest", [str(PYTHON), "-m", "pytest", "-q", "--junitxml=docs/R5_pytest.xml"], 300)
    shutil.copyfile(FRESH / "docs/R5_pytest.xml", ROOT / "docs/THUNDERBOLT_R5_pytest.xml")
elif phase == "serve":
    # Single child is the temporary application server; caller owns this command session.
    subprocess.run(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "railstorm.api:factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(CONTEXT["local_port"]),
        ],
        cwd=FRESH,
        env=ENV,
        check=True,
    )
else:
    raise ValueError("Unsupported phase")
