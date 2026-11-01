"""Bounded sequential R6 subprocesses on one isolated synthetic database."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import text

from experiments.r4_profile import resources
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"
BASE = json.loads((OUT / "baseline.json").read_text())
LOG = ROOT / ".work/r6/logs"
LOG.mkdir(exist_ok=True, parents=True)
source = Store()
database = BASE["database"]
assert database.startswith("railstorm_r6_") and len(database) == 21
url = source.engine.url.set(database=database).render_as_string(hide_password=False)
env = {**os.environ, "RAILSTORM_DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"}


def command(label, arguments, timeout=120):
    with (LOG / (label + ".log")).open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    print(json.dumps({"command": label, "returncode": process.returncode}), flush=True)
    if process.returncode:
        print((LOG / (label + ".log")).read_text()[-2500:])
        raise SystemExit(process.returncode)


def index_for(implementation):
    isolated = Store(url)
    with isolated.engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT current_database()").scalar_one() == database
        if implementation == "baseline":
            conn.exec_driver_sql("DROP INDEX IF EXISTS market_quote_lookup")
        else:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS market_quote_lookup ON events(run_id,(payload->>'pair'),seq DESC) WHERE event_type='MARKET_QUOTE'"
            )
    isolated.engine.dispose()


if sys.argv[1] == "provision":
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert not conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": database}).first()
        conn.exec_driver_sql('CREATE DATABASE "' + database + '"')
    command("migration", ["-m", "alembic", "upgrade", "head"])
elif sys.argv[1] == "bench":
    index_for(sys.argv[sys.argv.index("--implementation") + 1])
    command(sys.argv[2], ["-m", "experiments.r6_benchmark", "--tag", *sys.argv[2:]], 180)
elif sys.argv[1] == "campaign":
    for count in (25, 100, 500, 1000):
        for i, label in enumerate(("baseline", "modified", "modified", "baseline")):
            tag = f"matched-{count}-{i}-{label}"
            if (OUT / (tag + ".json")).exists():
                print(json.dumps({"retained_completed_measurement": tag}), flush=True)
                continue
            index_for(label)
            command(
                tag,
                [
                    "-m",
                    "experiments.r6_benchmark",
                    "--tag",
                    tag,
                    "--implementation",
                    label,
                    "--count",
                    str(count),
                ],
                180,
            )
    index_for("modified")
elif sys.argv[1] == "final1000":
    for i, label in enumerate(("baseline", "modified", "modified", "baseline")):
        tag = f"final-1000-{i}-{label}"
        assert not (OUT / (tag + ".json")).exists()
        index_for(label)
        command(
            tag,
            ["-m", "experiments.r6_benchmark", "--tag", tag, "--implementation", label, "--count", "1000"],
            180,
        )
    index_for("modified")
elif sys.argv[1] == "tests":
    assert resources()["ram_available"] > 512 * 1024**2, "Regression resource gate"
    index_for("baseline")
    command("migration_head", ["-m", "alembic", "upgrade", "head"])
    index_for("modified")
    validation = ROOT / ".work/r6/validation"
    validation.mkdir(exist_ok=False)
    for folder in ("railstorm", "tests", "experiments", "migrations", "scenarios", "scripts"):
        shutil.copytree(
            ROOT / folder, validation / folder, ignore=shutil.ignore_patterns("__pycache__", "reports")
        )
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copyfile(ROOT / name, validation / name)
    (validation / "docs/r4_evidence").mkdir(parents=True)
    for name in ("baseline_audit.py", "baseline_store.py"):
        shutil.copyfile(ROOT / "docs/r4_evidence" / name, validation / "docs/r4_evidence" / name)
    samples = []
    with (LOG / "pytest.log").open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "--junitxml=docs/THUNDERBOLT_R6_pytest.xml"],
            cwd=validation,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 300
        while process.poll() is None:
            sample = resources()
            samples.append(sample)
            if sample["ram_available"] < 256 * 1024**2 or time.monotonic() > deadline:
                process.terminate()
                process.wait(timeout=15)
                (OUT / "regression_resource_stop.json").write_text(json.dumps(samples, indent=2))
                raise RuntimeError("Regression stopped at memory/time safety gate")
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        returncode = process.returncode
    (OUT / "regression_resources.json").write_text(json.dumps(samples, indent=2))
    shutil.copyfile(validation / "docs/THUNDERBOLT_R6_pytest.xml", ROOT / "docs/THUNDERBOLT_R6_pytest.xml")
    shutil.copytree(validation / "docs", OUT / "validation_artifacts")
    print(json.dumps({"command": "fresh_validation_pytest", "returncode": returncode}), flush=True)
    if returncode:
        print((LOG / "pytest.log").read_text()[-3500:])
        raise SystemExit(returncode)
elif sys.argv[1] == "targeted":
    command(
        "targeted",
        [
            "-m",
            "pytest",
            "-q",
            "tests/test_r6_execution.py",
            "tests/test_transaction_boundary.py",
            "tests/test_r2_crashes.py",
            "tests/test_r21_finance.py",
            "tests/test_r21_integrity.py",
            "--junitxml=docs/THUNDERBOLT_R6_targeted.xml",
        ],
        120,
    )
elif sys.argv[1] == "contention":
    for implementation in ("baseline", "modified"):
        index_for(implementation)
        command(
            "contention-" + implementation,
            ["-m", "experiments.r6_contention", "--implementation", implementation],
            90,
        )
    index_for("modified")
else:
    raise ValueError("Unknown action")
