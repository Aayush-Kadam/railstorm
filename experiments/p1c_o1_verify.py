"""Fresh native migration and preservation checks, independent of efficacy cohort."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from zipfile import ZipFile

from railstorm.optimizer_experiments import compare
from railstorm.store import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    store = Store()
    name = "railstorm_o1_fresh_" + uuid.uuid4().hex[:8]
    with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = store.engine.url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "RAILSTORM_DATABASE_URL": url}
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    fresh = Store(url)
    comparison = compare(fresh, "OPT-01", 11, 1, persist=False, include_v2=True)
    with fresh.transaction(comparison["policies"][0]["physical_runs"]["control"]) as tx:
        head = tx.one("SELECT version_num FROM alembic_version")["version_num"]
    assert head == "007"
    assert all(r["metrics"]["completed"] == 1 for r in comparison["policies"])
    (ROOT / "docs/P1C_O1_postfix_fresh_database.json").write_text(
        json.dumps(
            {
                "database": name,
                "head": head,
                "verification_only": True,
                "migration_output": migration.stdout + migration.stderr,
                "comparison": comparison,
            },
            indent=2,
        )
    )
    preserved = json.loads((ROOT / "docs/P1C_O0_preserved_financial_sources.json").read_text())
    assert all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in preserved.items())
    with ZipFile(ROOT.parent / "RAILSTORM_P1C_O0_Review.zip") as archive:
        names = [n for n in archive.namelist() if n.startswith("railstorm/docs/P1C_O0")]
        names += [n for n in archive.namelist() if n.startswith("railstorm/experiments/reports/optimizer/")]
        for n in names:
            assert archive.read(n) == (ROOT.parent / n).read_bytes(), "HISTORICAL_EVIDENCE_CHANGED: " + n
    before = json.loads(
        (ROOT / "docs/P1C_O1_historical_archives_before.json").read_text(encoding="utf-8-sig")
    )
    archives = []
    for row in before:
        actual = hashlib.sha256(Path(row["Path"]).read_bytes()).hexdigest()
        assert actual == row["Hash"].lower()
        archives.append({"name": Path(row["Path"]).name, "sha256": actual})
    (ROOT / "docs/P1C_O1_postfix_preservation.json").write_text(
        json.dumps(
            {
                "unchanged_financial_sources": preserved,
                "historical_evidence_files_checked": len(names),
                "archives": archives,
            },
            indent=2,
        )
    )
    print(
        "Fresh migrations001-007, three-policy financial/replay verification, native-source and archive preservation passed"
    )


if __name__ == "__main__":
    main()
