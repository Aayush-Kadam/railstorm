"""New review archive; preserve all historical packages and reports."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
suite = ET.parse(docs / "THUNDERBOLT_R_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
prior = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
report = f"""# RAILSTORM — THUNDERBOLT-R FINAL ACCEPTANCE REPORT

Research preview: PARTIAL. Original vision: PARTIAL.

Source identity: SHA256_MANIFEST.json in the new review archive; no Git commit available.

## Implemented and verified

- Atomic router preflight now uses configured deterministic quote schedules, including quote ID/version/expiry, rather than static rates alone.
- Execution rejects changed atomic rates, quote identity, fees or proceeds before binding. An explicit new routing decision is required. Existing bound payments retain their durable execution contract.
- Four corridors execute through public routing API: USD/SGD, USD/EUR, USD/INR, EUR/SGD. Tests verify exact fees/proceeds, duplicate execution, independent audit, native reconcile and replay. Other rails retain their USD/SGD scope.
- Unfunded and unsupported route tests reject without financial execution. Scheduled quote replacement test rejects the stale decision and settles a new decision at the new version.
- Fixed three maintained-test typing errors without suppressions or changed validation coverage. `mypy --explicit-package-bases railstorm tests` passes 49 files. Legacy experiment scripts remain outside this maintained application/test gate; previous broad 44-error claim is not relabeled repository-wide type cleanliness.
- Fresh `.venv-thunderbolt` installed 38 locked packages using `uv sync --frozen --python .venv/Scripts/python.exe`, workspace cache and bounded network timeout. New environment passed fresh synthetic database migrations001–007 and CH-04 independent audit/replay, plus seven routing API tests. This does not yet certify clean frontend installation or browser startup from that environment.
- Consolidated regression: {suite["tests"]} tests, zero failures/errors/skips, {suite["time"]} seconds. One dependency deprecation warning. Evidence THUNDERBOLT_R_pytest.xml.

## Acceptance gates

| Gate | Result | Scope |
|---|---|---|
| Shared-router multicurrency | PASS | Four atomic corridors through public API; not universal rails |
| G11 FX full matrix | UNVERIFIED | Version-aware routing added; full twelve-case matrix incomplete |
| G12 adversarial expansion | UNVERIFIED | Existing real crash/contention retained; requested expansion incomplete |
| G14 browser | UNVERIFIED | No new complete browser matrix in this campaign |
| G15 clean install | UNVERIFIED | Backend dependencies and fresh migration smoke pass; frontend/browser clean install incomplete |
| G16 Docker | UNVERIFIED | Previously absent daemon pipe; not retried unchanged |
| G17 maintained types | PASS | Application and all maintained tests; historical scripts explicitly excluded |
| G18 matched optimization | UNVERIFIED | No performance change or matched benchmark |
| G19 10000 in 60 seconds | UNVERIFIED | Not run; no extrapolated claim |

No new performance claim. No external publication. Full original vision is not complete. Remaining workstreams remain authorized but unfinished, not demonstrated hard blockers. The smallest remaining preview gate is completing current-source browser acceptance and clean frontend setup; original vision also requires FX/adversarial/live-control/performance work.

## Commands

`python -m pytest -q --junitxml=docs/THUNDERBOLT_R_pytest.xml`

`python -m pytest -q tests/test_thunderbolt_routing.py --junitxml=docs/THUNDERBOLT_R_routing_pytest.xml`

`mypy --explicit-package-bases railstorm tests`

`python -m experiments.thunderbolt_fresh` using newly installed environment.

All thirteen historical archives verified against FINAL_prior_archives.json before packaging. Prior Finality review archive also preserved. CRC and every internal hash verified by the package script.
"""
(docs / "THUNDERBOLT_R_FINAL_ACCEPTANCE.md").write_text(report, encoding="utf-8")
files = []
for folder in ("railstorm", "tests", "migrations", "scenarios", "scripts", "experiments", "frontend/src"):
    files += [
        p
        for p in (root / folder).rglob("*")
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo", ".log")
        and not {"__pycache__", "reports", "work", "node_modules"}.intersection(p.parts)
    ]
files += [
    root / name
    for name in (
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "alembic.ini",
        "Dockerfile",
        "docker-compose.yml",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
    )
]
files += list(docs.glob("THUNDERBOLT_R_*"))
files += [docs / "FINAL_prior_archives.json"]
contents = {}
for path in sorted(set(files)):
    data = path.read_bytes()
    if path.suffix == ".xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if any(
        p in data for p in (b"C:" + b"\\Users\\", b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
    ):
        raise RuntimeError("PRIVATE_DATA: " + path.relative_to(root).as_posix())
    contents[path.relative_to(root).as_posix()] = data
manifest = {n: hashlib.sha256(d).hexdigest() for n, d in contents.items()}
target = root.parent / "RAILSTORM_THUNDERBOLT_R_REVIEW.zip"
if target.exists():
    raise FileExistsError(target)
with ZipFile(target, "w", ZIP_DEFLATED) as z:
    for name, data in contents.items():
        z.writestr("railstorm/" + name, data)
    z.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(manifest, indent=2))
with ZipFile(target) as z:
    assert z.testzip() is None
    for name, sha in manifest.items():
        assert hashlib.sha256(z.read("railstorm/" + name)).hexdigest() == sha
print(
    json.dumps(
        {
            "files": len(manifest) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "historical_archives_unchanged": len(prior),
        }
    )
)
