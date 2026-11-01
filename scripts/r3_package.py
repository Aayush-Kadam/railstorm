"""Build the R3 evidence index and verify the local review archive."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
suite = ET.parse(docs / "THUNDERBOLT_R3_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
browser = json.loads((docs / "THUNDERBOLT_R3_browser.json").read_text())
assert not browser["pageErrors"] and len(browser["records"]) == 10
prior = json.loads((docs / "THUNDERBOLT_R3_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
original = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in original:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
fresh = root / ".work/r3frontend"
assert (
    hashlib.sha256((root / "frontend/pnpm-lock.yaml").read_bytes()).digest()
    == hashlib.sha256((fresh / "pnpm-lock.yaml").read_bytes()).digest()
)
installation = {
    "package_manager": "pnpm11.25.0",
    "node": "24.22.1",
    "directory": ".work/r3frontend",
    "fresh_dependency_tree": True,
    "downloaded_packages": 26,
    "method": "frozen lockfile; dedicated workspace store and temporary directory; pinned esbuild approved; CI=true offline completion",
    "commands": [
        "pnpm --dir .work/r3frontend install --frozen-lockfile --store-dir ../r3-pnpm-store --fetch-retries 0 --fetch-timeout 20000",
        "pnpm --dir .work/r3frontend approve-builds esbuild",
        "CI=true pnpm --dir .work/r3frontend install --frozen-lockfile --store-dir ../r3-pnpm-store --offline",
        "CI=true pnpm --dir .work/r3frontend run build",
        "node ../.work/r3frontend/r3.e2e.mjs (working directory frontend; loopback backend)",
    ],
    "lockfile_sha256": hashlib.sha256((fresh / "pnpm-lock.yaml").read_bytes()).hexdigest(),
    "build_files": {
        p.relative_to(fresh / "dist").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (fresh / "dist").rglob("*")
        if p.is_file()
    },
    "build_result": "TypeScript and Vite production build PASS; large chunk warning retained",
    "browser_result": "10 workflows covering BR01–BR15; zero page errors; expected409 console response retained",
    "limitations": "Fresh frontend dependency tree on existing Windows/Node installation; existing Chromium browser binary reused. Not a clean OS or container verification.",
}
(docs / "THUNDERBOLT_R3_frontend_install.json").write_text(json.dumps(installation, indent=2))
matrix = [
    ("01", "Atomic SETTLED; native/independent audit and replay", "atomic"),
    ("02", "Correspondent SETTLED after native step advances", "correspondent"),
    ("03", "Instant COMPLETED with beneficiary credit", "instant"),
    ("04", "Shared-router atomic USD/EUR output9200, bound ATOMIC_PVP", "multicurrency"),
    ("05", "Prepared atomic quote expires before commitment: EXPIRED", "expired"),
    ("06", "Source shortage REJECTED before settlement", "shortage"),
    ("07", "Source final true, destination final false, obligation13300", "outage-pending"),
    ("08", "Live fault persisted DESTINATION_DOWN; native delivery unavailable", "outage-pending"),
    ("09", "Restore then native recover; persists across refresh", "restored-recovered-duplicate"),
    (
        "10",
        "Retry after recovery; beneficiary13300 and exactly2 payment journal groups",
        "restored-recovered-duplicate",
    ),
    ("11", "Lost ACK observed in stored messages then native recovery", "lost-ack-recovery"),
    (
        "12",
        "Initialize, prepare, inject outage, advance source/destination, restore/recover",
        "outage-pending",
    ),
    ("13", "UI audit PASS only after executed native/independent checks", "all six baseline scenarios"),
    ("14", "UI replay PASS and durable replay.match asserted", "all six baseline scenarios"),
    ("15", "Restore before preparation rejected409; zero payments persisted", "invalid-transition"),
]
table = "| Browser ID | Result | Executed assertion | Evidence record |\n|---|---|---|---|\n" + "\n".join(
    f"| BR-{i} | PASS | {description} | {record} |" for i, description, record in matrix
)
common = f"\n\nRegression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds; one existing Starlette/AnyIO deprecation warning. Maintained Python lint/format/type checks pass. Source revision is SHA256_MANIFEST.json in archive, not an invented Git commit.\n"
reports = {
    "LIVE_CRISIS_LAB": """LIVE CRISIS LAB: PASS for bounded server-owned scenarios.

Implemented LiveCrisis service and loopback-only API. Each session is a native financial world with a unique ID, scenario, seed, UTC creation time, stage, payment IDs, fault state, recovery status, audit/replay results and event references. Session updates are durable CRISIS_SESSION events; no client-invented financial projections. Dedicated PostgreSQL advisory locks serialize orchestration across server processes; existing financial run-row locks/state guards remain authoritative. Maximum120 actions per session. No schema migration or settlement engine rewrite.

Initialize, inspect, advance, inject supported destination outage/lost ACK, restore, retry/recover, native+independent audit and replay are executable. Manual advance calls exactly one native workflow operation. Legal controls are bounded; illegal transitions return409. Fault state drives actual native message delivery availability/ACK behavior. Atomic scenes use persisted shared-router decisions; correspondent and instant scenes use native commands. Recorded scenario playback remains below the live section, labeled recorded. Local browser state stores only the session identifier; backend persists financial state and operator fault state. Seed records deterministic fixed scenario inputs, not random market data.

Views show five institutions/four currency definitions, supported corridor scope, actual account liquidity, selected rail/route, quote terms, reservations, journals, finality, obligations, recovery state and events. Audit/replay statuses appear only after an executed check and are cleared after financial/control actions. Verification objects record the audited event head; saving metadata appends another session event. They certify the displayed executed checkpoint, not a promise about future external mutations.

Demo: open Crisis lab; select Instant; Initialize; Advance to prepare; Inject destination outage; Advance source; Advance destination; inspect13300 obligation; reload; Restore participant; Recover; Retry; Run financial audit; Replay durable history.
""",
    "BROWSER_MATRIX": table
    + "\n\nExecutable test frontend/r3.e2e.mjs launches real Chromium against loopback FastAPI serving the fresh production build. THUNDERBOLT_R3_browser.json stores actual backend inspections after UI operations, route decisions, financial state, messages, audit/replay and console logs. Zero page runtime errors. One expected console resource409 comes from BR15 and is retained. Screenshot evidence: screenshots/r3-live-pending.png and r3-live-recovered.png. This is15 assertions grouped into10 workflows, not15 independent browser processes.",
    "FRONTEND_INSTALL": """FRESH FRONTEND INSTALLATION: PASS on the supported existing Windows/Node host.

Created .work/r3frontend with copied source/config and original lockfile, no preexisting node_modules; installed26 packages into a new workspace store. Initial install downloaded and linked packages but returned ERR_PNPM_IGNORED_BUILDS for esbuild. Approved only pinned esbuild with pnpm approve-builds esbuild. A noninteractive module-directory confirmation then required documented CI=true mode and the same store; frozen offline completion returned0. Source frontend/pnpm-workspace.yaml records the tested allowBuilds esbuild policy. No lockfile pins removed, supply-chain checks bypassed or system permissions changed. pnpm11.25.0/Node24.22.1.

pnpm run build in that fresh directory ran TypeScript and Vite successfully; source/fresh lock hashes match. Fresh dist was copied into the backend's existing frontend/dist static-serving location and served over loopback. Browser test module was copied into the fresh project and imported newly installed Playwright dependencies. Existing Chromium executable reused. The final visual-panel CSS was rebuilt and the complete browser matrix rerun. Large JavaScript chunk warning remains; no bundle-performance claim. This verifies fresh frontend dependencies/build/connectivity/browser workflow, not clean OS/backend/container installation. Full machine-readable command/hash record: THUNDERBOLT_R3_frontend_install.json.
""",
    "API_VERIFICATION": """API verification: PASS. Eight new Python tests cover six scenario modes, outage/restoration/repeated recovery/illegal transitions and external-origin rejection. Endpoints: GET /api/v1/live-crisis/scenarios; POST /sessions with scenario/seed; GET /sessions/{id}; POST /sessions/{id}/actions with a strict action enum. Prefix /api/v1/live-crisis. Actions ADVANCE, OUTAGE, LOSE_ACK, RESTORE, RETRY, RECOVER, AUDIT, REPLAY. Loopback clients only for mutations; external web origins rejected. No arbitrary financial mutation parameters or user-specified SQL. Read inspections do not mutate native state; they acquire authoritative snapshots.

Commands: python -m pytest -q tests/test_r3_live.py --junitxml=docs/THUNDERBOLT_R3_api.xml; python -m pytest -q --junitxml=docs/THUNDERBOLT_R3_pytest.xml; ruff check railstorm tests; ruff format --check railstorm tests; mypy --explicit-package-bases railstorm tests. Start: uvicorn railstorm.api:factory --factory --host127.0.0.1 --port8859 (use separate --host 127.0.0.1 --port 8859 arguments). Browser: RAILSTORM_URL=http://127.0.0.1:8859 node ../.work/r3frontend/r3.e2e.mjs from frontend. Test scripts use the existing synthetic PostgreSQL environment variable; credentials excluded.
""",
    "FINANCIAL_SAFETY": """Native ledger engines unchanged. Live atomic execution uses existing shared-router preparation/execution. Correspondent steps use native hop finality. Instant controls invoke native durable messages and recovery. Source-final pending state is visibly distinct from completed beneficiary delivery. Audit invokes native reconcile and independent SQL audit separately; replay invokes the existing canonical replay path. Browser assertions verify exact EUR proceeds9200, pending SGD obligation13300, restored beneficiary13300 and exactly two financial journal groups after duplicate retries. Loss of acknowledgment is observed before recovery. Expiry and source shortage reject safely with native states. Full regression retains G11/G12 concurrency, crash, quote and corruption tests.

Limitations: new live fault controls are destination outage and lost ACK on instant-link sessions. Recorded compound scenarios retain their predeclared faults and are not mislabeled live. This is a local synthetic operator service, not production authorization or a public financial API. No Docker or throughput work in this milestone.
""",
    "FINAL_GO_NO_GO": """LIVE CRISIS LAB: PASS.

G14 BROWSER ACCEPTANCE: PASS for BR01–BR15 as indexed in the browser matrix.

FRESH FRONTEND INSTALLATION: PASS on the tested Windows/Node/pnpm host and new dependency tree.

All eight new API tests and the integrated current-source suite pass. Independent/native audit and replay pass at displayed verification checkpoints. G11/G12 regression coverage retained. Finite synthetic workflow scope and operator limits remain explicit. No claim of production readiness, Docker readiness, every possible fault, or throughput target achievement. Existing large-bundle warning remains. Next acceptance workstream is separately bounded performance engineering and remaining installation/container release gates; no additional frontend redesign is required for this accepted scope. Historical archive checksums are verified before packaging; CRC and all source/evidence manifest entries verified afterward.
""",
}
for name, body in reports.items():
    (docs / f"THUNDERBOLT_R3_{name}.md").write_text(
        "# THUNDERBOLT-R3 " + name.replace("_", " ") + "\n\n" + body + common
    )
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
    root / n
    for n in (
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "alembic.ini",
        "Dockerfile",
        "docker-compose.yml",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/pnpm-workspace.yaml",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/r3.e2e.mjs",
    )
]
files += list(docs.glob("THUNDERBOLT_R3_*")) + list((docs / "screenshots").glob("r3-*.png"))
files += [docs / "IMPLEMENTATION_STATUS.md", docs / "FINAL_prior_archives.json"]
contents = {}
for p in sorted(set(files)):
    if not p.is_file():
        continue
    data = p.read_bytes()
    if p.suffix == ".xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if any(
        pattern in data
        for pattern in (b"C:" + b"\\Users\\", b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
    ):
        raise RuntimeError("PRIVATE_DATA: " + p.relative_to(root).as_posix())
    contents[p.relative_to(root).as_posix()] = data
for p in (fresh / "dist").rglob("*"):
    if p.is_file():
        contents["frontend/dist/" + p.relative_to(fresh / "dist").as_posix()] = p.read_bytes()
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
target = root.parent / "RAILSTORM_THUNDERBOLT_R3_REVIEW.zip"
if target.exists():
    raise FileExistsError(target)
with ZipFile(target, "w", ZIP_DEFLATED) as z:
    for name, data in contents.items():
        z.writestr("railstorm/" + name, data)
    z.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(manifest, indent=2))
with ZipFile(target) as z:
    assert z.testzip() is None
    assert all("docs/THUNDERBOLT_R3_" + name + ".md" in manifest for name in reports)
    for name, sha in manifest.items():
        assert hashlib.sha256(z.read("railstorm/" + name)).hexdigest() == sha
print(
    json.dumps(
        {
            "files": len(manifest) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "prior_archives_verified": len(prior),
        }
    )
)
