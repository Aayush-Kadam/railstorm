"""Assemble truthful finality reports and a hash-verified local review package."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
suite = ET.parse(docs / "FINALITY_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
prior = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
common = (
    f"Current consolidated regression: {suite['tests']} passed, zero failures/errors/skips, "
    f"{suite['time']} seconds. See FINALITY_pytest.xml. This is a synthetic local laboratory. "
    "Fixed-order routing remains default; V1/V2 remain experimental with historical mixed/no-advantage conclusions. "
    "No new performance optimization or matched speedup is claimed. Docker engine unavailable; container smoke UNVERIFIED."
)
matrix = {
    "G01": ("Five modeled institutions", "PASS", "domain.BANKS and funded corridor fixtures"),
    "G02": ("Four currency definitions", "PASS", "Currency enum and atomic corridor fixtures"),
    "G03": (
        "Funded multicurrency corridors",
        "PASS",
        "Native atomic USD/SGD, USD/EUR, USD/INR, EUR/SGD only; shared-router breadth unverified",
    ),
    "G04": ("Atomic correctness", "PASS", "Current regression and new exact corridor fixtures"),
    "G05": ("Correspondent correctness", "PASS", "Existing native regression; USD/SGD scope"),
    "G06": ("Instant separate finality", "PASS", "Existing native regression; USD/SGD scope"),
    "G07": ("Conservation and fees", "PASS", "Native/independent checks in current fixtures"),
    "G08": ("Routing and replay", "PASS", "Existing regression; expanded currency routing unverified"),
    "G09": ("Liquidity concurrency", "PASS", "Real database-session contention test retained"),
    "G10": ("Crash recovery", "PASS", "Disposable-database worker exit/restart test retained"),
    "G11": (
        "Dynamic quote versions",
        "UNVERIFIED",
        "Bounded atomic schedule test passes; full FX-01–FX-10 matrix and source-final instant versioning incomplete",
    ),
    "G12": (
        "Expanded adversarial scenarios",
        "UNVERIFIED",
        "Existing coverage retained; requested expanded matrix incomplete",
    ),
    "G13": (
        "Independent audit controls",
        "PASS",
        "Existing negative controls and corridor independent_audit",
    ),
    "G14": (
        "Live browser workflows",
        "UNVERIFIED",
        "See current browser artifact if present; entire required ten-workflow matrix incomplete",
    ),
    "G15": (
        "Fresh local installation",
        "UNVERIFIED",
        "Fresh migration smoke is separate from clean dependency installation",
    ),
    "G16": ("Docker payment smoke", "UNVERIFIED", "Docker engine named pipe unavailable"),
    "G17": (
        "Supported quality gates",
        "UNVERIFIED",
        "Application/new tests type-clean; maintained historical test errors remain",
    ),
    "G18": ("Matched performance", "UNVERIFIED", "No optimization; bounded unchanged-code profile only"),
    "G19": ("10000 payments in 60 seconds", "UNVERIFIED", "Not run; no extrapolated claim"),
    "G20": ("Review archive", "PASS", "CRC and all internal manifest hashes checked by this script"),
}
table = "| Gate | Requirement | Result | Scope |\n|---|---|---|---|\n" + "\n".join(
    f"| {key} | {a} | {b} | {c} |" for key, (a, b, c) in matrix.items()
)
texts = {
    "RAILSTORM_FINAL_EXECUTIVE_SUMMARY": "Research preview PARTIAL; expanded vision PARTIAL. Implemented four native atomic corridor fixtures and opt-in immutable deterministic quote schedules. Full requested integration and reproducibility gates remain incomplete.",
    "RAILSTORM_FINAL_GO_NO_GO": "Research Preview: PARTIAL. Original Expanded Vision: PARTIAL. No confirmed new financial safety violation observed. Freeze verified changes for review; do not advertise release readiness.\n\n"
    + table,
    "RAILSTORM_ARCHITECTURE": "PostgreSQL event-sourced ledger; run-row locking serializes world mutations. Exact integer journals and reservations enforce funded transitions. Atomic PvP commits both legs in one transaction. Correspondent hops retain independent finality and nostro/vostro obligations. Instant link has separate domestic stages and funded delivery obligations. Routing persists binding decisions. Native reconciliation reads authoritative projections and verifies full event replay. Independent SQL audit is a separate bounded verification path. React/Vite Command Center calls local FastAPI operations.",
    "RAILSTORM_BANK_AND_CORRIDOR_MATRIX": "Atlas US, Sterling UK, Rhine EU, Meridian SG, Bharat IN. Four currency definitions: USD/EUR/SGD/INR. New native atomic funded fixtures: Atlas USD to Meridian SGD (133/100), Atlas USD to Rhine EUR (92/100), Atlas USD to Bharat INR (83/1), Rhine EUR to Meridian SGD (145/100), with Sterling as FX provider. Principal 10001 source minor units, source fee 25, floor rounding. These are deterministic synthetic fixture rates, not live markets. Correspondent and instant link remain demonstrated USD/SGD. Expanded shared-router and browser corridor selection is unverified.",
    "RAILSTORM_FINANCIAL_INVARIANTS": "Preserved: balanced currency-specific journals, authoritative liquidity/reservations, durable decisions, funded obligations, idempotency, independent hop/domestic finality, full projection checks and event-chain replay. No event-head cache or audit bypass. New schedule quote metadata is stored with the locked quote and event; settlement uses the persisted quote. No silent financial repair.",
    "RAILSTORM_RAIL_EXECUTION_MATRIX": "ATOMIC_PVP: native multicurrency fixtures plus existing USD/SGD routed flow. CORRESPONDENT: existing USD/SGD native and routed workflows. INSTANT_LINK: existing USD/SGD native and routed workflows. Do not infer universal currency/rail connectivity.",
    "RAILSTORM_FAULT_AND_RECOVERY_MATRIX": "Existing real concurrent contention, worker process exit during recovery, repeated recovery, lost ACK, duplicate/stale/premature messages, participant outages, shortage and corruption controls retained. Full requested crash-boundary and cross-institution permutation coverage is not established. Recorded Crisis Lab stages remain recorded evidence.",
    "RAILSTORM_INDEPENDENT_AUDIT_REPORT": "New corridor tests invoke independent_audit in addition to native reconcile and replay_check. Existing independent controls remain in regression. Shared assumptions include account identifiers, integer amounts and modeled rail contracts; this is not an external certification. See test_final_corridors.py and test_final_fx.py for exact assertions.",
    "RAILSTORM_PERFORMANCE_REPORT": "Unchanged-code fixed-order OPT-01 profiled wall: 5 payments 1.5369s; 10 payments 2.5407s; 25 payments 8.1568s. SQL statements 950/1900/4618. Raw FINAL_bounded_profile.json includes overlapping inclusive function timings. Historical 100-payment 719.96s is not a matched comparison. No speedup or 10000/60s achievement established; memory comparisons absent.",
    "RAILSTORM_REPRODUCIBILITY_REPORT": "Existing local Python/PostgreSQL environment executes current tests. Fresh database smoke, if present in FINALITY_fresh_database.json, proves migrations and native crisis audit/replay only. Clean backend/frontend dependency installation remains unverified. Existing frontend installed-dependency build passed. Docker engine unavailable; do not treat Compose as executed.",
    "RAILSTORM_BROWSER_VERIFICATION": "Current instant browser evidence, if present: FINALITY_instant_browser.json and screenshots finality-instant-*.png. This verifies only the assertions in frontend/finality-instant.e2e.mjs. Entire requested ten-workflow matrix remains incomplete. Build success alone is not browser acceptance. Crisis Lab recorded playback is explicitly recorded; live instant inspector has actual mutation/recovery operations.",
    "RAILSTORM_LIMITATIONS_AND_FUTURE_WORK": "Synthetic identities, balances, rates and legal finality; no banking connectivity or compliance certification. Limited shared-router currency breadth. Atomic scheduled quotes are opt-in config and not a universal market-version service. FX schedule validity expires prepared quotes; finalized postings are immutable. Full FX acceptance matrix, expanded adversarial matrix, live Crisis Lab integration, clean installation, repository-wide maintained types and matched optimization remain incomplete. Container and original throughput target unverified.",
    "RAILSTORM_DEMO_SCRIPT": "Use the existing local setup in README. Open Instant link: Create instant payment; enable Destination outage; Deliver source; Deliver destination; inspect source-final/destination-pending and obligation. Reload to verify persistence. Disable outage; Operator recovery/retry twice; retry destination; Reconcile. Beneficiary should remain credited once. Atomic and correspondent inspectors provide their respective existing workflows. Run pytest tests/test_final_corridors.py tests/test_final_fx.py for new backend corridor/quote demonstrations. Do not present these tests as new UI controls.",
    "RAILSTORM_COMMANDS_AND_EVIDENCE": "Commands: python -m pytest -q --junitxml=docs/FINALITY_pytest.xml; ruff check railstorm tests/test_final_corridors.py tests/test_final_fx.py; mypy railstorm tests/test_final_corridors.py tests/test_final_fx.py; node frontend/node_modules/typescript/bin/tsc --noEmit (from frontend use node node_modules/typescript/bin/tsc); node node_modules/vite/bin/vite.js build from frontend; node finality-instant.e2e.mjs with RAILSTORM_URL. PostgreSQL URL supplied privately through RAILSTORM_DATABASE_URL; no credentials packaged. Prior archive hashes checked against FINAL_prior_archives.json.",
    "RAILSTORM_COMPLETION_LEDGER": "Changed railstorm/ledger.py: opt-in schedule validation, immutable quote version metadata and selection; settlement contract retained. Added test_final_corridors.py (four cases), test_final_fx.py (replacement/expiry/immutable settlement). Added finality browser/fresh-database runners and packaging script. Existing final completion ledger retained. This package is reviewable partial work; no release readiness claim. Outstanding gates are explicitly listed in final decision matrix.",
}
for name, body in texts.items():
    (docs / (name + ".md")).write_text(
        "# " + name.replace("_", " ") + "\n\n" + body + "\n\n" + common + "\n", encoding="utf-8"
    )
status = root / "docs/IMPLEMENTATION_STATUS.md"
old_status = status.read_text(encoding="utf-8")
if not old_status.startswith("# Operation Finality"):
    status.write_text(
        "# Operation Finality\n\nPARTIAL. Four funded native atomic corridors and deterministic atomic quote schedule added and tested. See RAILSTORM_FINAL_GO_NO_GO.md.\n\n"
        + old_status,
        encoding="utf-8",
    )
readme = root / "README.md"
old = readme.read_text(encoding="utf-8")
if "Operation Finality current scope" not in old:
    start = old.find("## Native Windows")
    setup = old[start : old.find("## Prove the kernel", start)]
    readme.write_text(
        "# RAILSTORM\n\n## Operation Finality current scope\n\nSynthetic, PostgreSQL-backed cross-border settlement laboratory with three executable modeled rails: atomic PvP, correspondent banking and linked instant payments. Research preview readiness: **PARTIAL**.\n\nFive modeled banks and four currency definitions. Native atomic fixtures verify USD→SGD, USD→EUR, USD→INR and EUR→SGD; correspondent and instant demonstrations remain USD→SGD. Fixed-order routing is default. V1/V2 are experimental; no universal optimizer advantage demonstrated.\n\n"
        + common
        + "\n\nSee [final decision](docs/RAILSTORM_FINAL_GO_NO_GO.md), [architecture](docs/RAILSTORM_ARCHITECTURE.md), [scope](docs/RAILSTORM_BANK_AND_CORRIDOR_MATRIX.md), and [demo guide](docs/RAILSTORM_DEMO_SCRIPT.md).\n\n"
        + setup
        + "\n## Tests\n\nRun `python -m pytest -q`. New corridor and quote fixtures: `python -m pytest -q tests/test_final_corridors.py tests/test_final_fx.py`. Clean dependency installation is not yet freshly certified. No public deployment or real-money integration is provided.\n",
        encoding="utf-8",
    )
files = []
for folder in ("railstorm", "migrations", "scenarios", "tests", "experiments", "scripts", "frontend/src"):
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
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/finality-instant.e2e.mjs",
    )
]
files += (
    list(docs.glob("RAILSTORM_*.md"))
    + list(docs.glob("FINAL*.json"))
    + list(docs.glob("FINAL*.xml"))
    + list((docs / "screenshots").glob("finality-*.png"))
)
contents = {}
for path in sorted(set(files)):
    data = path.read_bytes()
    if path.suffix == ".xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if any(
        p in data for p in (b"C:" + b"\\Users\\", b"BEGIN " + b"PRIVATE KEY", b"PRIVATE-HOSTNAME-PLACEHOLDER")
    ):
        raise RuntimeError("PRIVATE_DATA: " + path.relative_to(root).as_posix())
    contents[path.relative_to(root).as_posix()] = data
manifest = {n: hashlib.sha256(d).hexdigest() for n, d in contents.items()}
target = root.parent / "RAILSTORM_FINAL_FLAGSHIP_REVIEW.zip"
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
    assert all("docs/" + name + ".md" in manifest for name in texts)
print(
    json.dumps(
        {
            "files": len(manifest) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "prior_archives_unchanged": len(prior),
        }
    )
)
