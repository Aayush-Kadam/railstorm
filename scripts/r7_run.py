"""Bounded R7 runs against only a fresh synthetic database context."""

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
OUT = ROOT / "docs/r7_evidence"
LOG = ROOT / ".work/r7/logs"
LOG.mkdir(exist_ok=True, parents=True)
context = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
database = context["database"]
assert database.startswith("railstorm_r7_") and len(database) == 21
source = Store()
url = source.engine.url.set(database=database).render_as_string(hide_password=False)
env = {**os.environ, "RAILSTORM_DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"}


def command(label, arguments, timeout=180, cwd=ROOT):
    log = LOG / (label + ".log")
    if log.exists():
        raise FileExistsError(log)
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(
            [sys.executable, *arguments],
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    print(json.dumps({"command": label, "returncode": result.returncode}), flush=True)
    if result.returncode:
        print(log.read_text(encoding="utf-8")[-2500:])
        raise SystemExit(result.returncode)


def validation(label):
    target = ROOT / ".work/r7" / label
    target.mkdir(exist_ok=False)
    for folder in ("railstorm", "tests", "experiments", "scripts", "migrations", "scenarios"):
        shutil.copytree(
            ROOT / folder, target / folder, ignore=shutil.ignore_patterns("__pycache__", "reports")
        )
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copyfile(ROOT / name, target / name)
    (target / "docs/r4_evidence").mkdir(parents=True)
    for name in ("baseline_audit.py", "baseline_store.py"):
        shutil.copyfile(ROOT / "docs/r4_evidence" / name, target / "docs/r4_evidence" / name)
    return target


phase = sys.argv[1]
if phase == "provision":
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert not conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": database}).first()
        conn.exec_driver_sql('CREATE DATABASE "' + database + '"')
    command("migration", ["-m", "alembic", "upgrade", "head"])
elif phase == "bench":
    command(sys.argv[2], ["-m", "experiments.r7_benchmark", "--tag", *sys.argv[2:]], 240)
elif phase in {"ladder", "accepted", "wait500"}:
    if phase == "wait500":
        samples = []
        deadline = time.monotonic() + 240
        consecutive = 0
        while consecutive < 3:
            sample = resources()
            samples.append(sample)
            consecutive = consecutive + 1 if sample["ram_available"] > 2176 * 1024**2 else 0
            if time.monotonic() > deadline:
                (OUT / "wait500_resource_stop.json").write_text(
                    json.dumps(samples, indent=2), encoding="utf-8"
                )
                raise RuntimeError("No stable major-run headroom within4 minutes; no benchmark started")
            time.sleep(1)
        (OUT / "wait500_resources.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
    count = sys.argv[2]
    assert count in {"100", "500", "1000", "2500", "5000", "10000"}
    for i, implementation in enumerate(("baseline", "modified", "modified", "baseline")):
        prefix = (
            sys.argv[3]
            if len(sys.argv) > 3
            else "window1"
            if phase == "wait500"
            else "accepted"
            if phase == "accepted"
            else "matched"
        )
        tag = f"{prefix}-{count}-{i}-{implementation}"
        command(
            tag,
            [
                "-m",
                "experiments.r7_benchmark",
                "--tag",
                tag,
                "--implementation",
                implementation,
                "--count",
                count,
            ],
            240,
        )
elif phase == "targeted":
    target = validation(sys.argv[2])
    command(
        sys.argv[2],
        [
            "-m",
            "pytest",
            "-q",
            "tests/test_r7_native_sql.py",
            "tests/test_initial.py",
            "tests/test_transaction_boundary.py",
            "tests/test_r6_execution.py",
            "tests/test_p01_regressions.py",
            "--junitxml=docs/R7_targeted.xml",
        ],
        180,
        target,
    )
    shutil.copyfile(target / "docs/R7_targeted.xml", OUT / (sys.argv[2] + ".xml"))
elif phase == "tests":
    assert resources()["ram_available"] > 1024**3, "Regression start resource gate"
    target = validation("validation-final")
    samples = []
    with (LOG / "full_pytest.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "--junitxml=docs/R7_pytest.xml"],
            cwd=target,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 420
        while process.poll() is None:
            sample = resources()
            samples.append(sample)
            if sample["ram_available"] < 512 * 1024**2 or time.monotonic() > deadline:
                process.terminate()
                process.wait(timeout=15)
                (OUT / "regression_resource_stop.json").write_text(
                    json.dumps(samples, indent=2), encoding="utf-8"
                )
                raise RuntimeError("Regression stopped at memory/time safety gate")
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    (OUT / "regression_resources.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
    shutil.copyfile(target / "docs/R7_pytest.xml", ROOT / "docs/R7_pytest.xml")
    shutil.copytree(target / "docs", OUT / "validation_artifacts")
    print(json.dumps({"command": "full_pytest", "returncode": process.returncode}), flush=True)
    if process.returncode:
        print((LOG / "full_pytest.log").read_text(encoding="utf-8")[-3500:])
        raise SystemExit(process.returncode)
elif phase == "concurrency":
    for implementation in ("baseline", "modified"):
        command(
            "concurrency-" + implementation,
            ["-m", "experiments.r7_concurrency", "--implementation", implementation],
            180,
        )
elif phase == "protocol":
    for implementation in ("baseline", "modified"):
        command(
            "protocol-" + implementation,
            ["-m", "experiments.r7_protocol", "--implementation", implementation],
            60,
        )
else:
    raise ValueError("Unknown action")
