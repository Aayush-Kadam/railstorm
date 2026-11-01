"""Validate final local/container acceptance and create a private review archive."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = DOCS / "r5_evidence"


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8-sig"))


suite = ET.parse(DOCS / "THUNDERBOLT_R5_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
local = read("local_smoke.json")
container = read("container_smoke.json")
environment = read("environment.json")
quality = read("quality.json")
database = read("local_database.json")
prepared = read("fresh_source_manifest.json")
assert prepared["initial_virtual_environment_absent"] and prepared["initial_node_modules_absent"]
assert (
    database["initial_public_table_count"] == 0
    and database["migration_head"] == "007"
    and database["loaded_fixtures"] == 19
)
assert local["phase"] == container["phase"] == "RESTART_PASS"
assert (
    local["application_restart_verified"]
    and not local["database_restart_verified"]
    and local["restart_scope"] == "backend"
)
assert (
    container["application_restart_verified"]
    and container["database_restart_verified"]
    and container["restart_scope"] == "stack"
)
for smoke in (local, container):
    for record in smoke["records"]:
        check = record["checks"]
        assert (
            check["native"]["financial_errors"] == 0
            and check["independent_worker_SQL_audit"]["pass"]
            and check["replay"]["match"]
        )
    for key in ("pending_checks", "quote_checks", "final_checks"):
        check = smoke[key]
        assert (
            check["native"]["financial_errors"] == 0
            and check["independent_worker_SQL_audit"]["pass"]
            and check["replay"]["match"]
        )
for name in ("local_browser.json", "container_browser.json"):
    browser = read(name)
    assert not browser["pageErrors"] and len(browser["records"]) == 10
assert all(quality[key]["returncode"] == 0 for key in ("lint", "format", "maintained_types"))
assert (
    environment["financial_sources_match_verified_R4"]
    and environment["local_and_container_frontend_bytes_identical"]
)
assert not environment["image_checks"]["source_bytecode"]
assert environment["local"]["isolated_source"] and not environment["local"]["system_site_packages_included"]
commands = read("docker_commands.json")
assert all(value["returncode"] == 0 and not value["timeout"] for value in commands.values())
prior = read("prior_archives.json")
original = json.loads((DOCS / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for archive in prior + original:
    assert hashlib.sha256((ROOT.parent / archive["name"]).read_bytes()).hexdigest() == archive["sha256"]

# Export only selected, sanitized command logs, never the private installation tree.
logs = OUT / "logs"
logs.mkdir(exist_ok=True)
for path in (ROOT / ".work/r5/logs").glob("*.log"):
    content = path.read_text(encoding="utf-8", errors="replace")
    content = re.sub(re.escape(str(ROOT)), "<WORKSPACE>", content, flags=re.IGNORECASE)
    content = re.sub(re.escape(ROOT.as_posix()), "<WORKSPACE>", content, flags=re.IGNORECASE)
    (logs / path.name).write_text(content, encoding="utf-8")

diagnostics = {
    "initial_restricted_error": "permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine",
    "approved_host_access": "PASS; Docker Desktop processes observed running and engine29.9.2 accessible without modifying ACLs or OS settings",
    "context": environment["docker_context"],
    "engine": environment["docker_engine"],
    "cli": environment["docker_cli"],
    "compose": environment["compose"],
    "deployment_defects_fixed": [
        "recursive host cache/extra environment exclusion",
        "loopback Docker gateway compatibility with explicit opt-in",
        "application health check and evidence volume",
    ],
    "user_action_required": None,
}
(OUT / "docker_diagnostics.json").write_text(json.dumps(diagnostics, indent=2))
installation = {
    "backend_packages_installed": 38,
    "frontend_packages_installed": 26,
    "fresh_directories": True,
    "cache_policy": "Only downloaded wheel/package caches reused; no previous virtual environment or node_modules copied; system site-packages disabled",
    "commands": [
        "python scripts/r5_prepare.py",
        "uv --directory .work/r5/source sync --frozen --offline --python <existing-supported-python> --cache-dir .work/uv-thunderbolt-cache",
        "CI=true pnpm --dir .work/r5/source/frontend install --frozen-lockfile --offline --store-dir .work/r3-pnpm-store",
        "python scripts/r5_local.py provision (fresh environment runs alembic upgrade head and scripts/load_scenarios.py)",
        "CI=true pnpm --dir .work/r5/source/frontend run build",
        "fresh-python -m uvicorn railstorm.api:factory --factory --host127.0.0.1 --port<context.local_port> (separate --host/--port arguments)",
        "python scripts/r5_local.py initial; restart temporary backend; python scripts/r5_local.py restart",
        "RAILSTORM_URL=http://127.0.0.1:<context.local_port> node r3.e2e.mjs from fresh frontend",
        "python scripts/r5_local.py tests; python scripts/r5_quality.py",
    ],
    "scope": "Fresh application checkout, dependencies and empty synthetic database on an existing supported Windows/Python/Node/PostgreSQL host, not a new OS install. Existing Playwright Chromium binary reused.",
}
(OUT / "installation.json").write_text(json.dumps(installation, indent=2))

ct = [
    ("01", "Atomic native SETTLED; exact journal/replay audit", "records:atomic"),
    ("02", "Correspondent native SETTLED after independent hop advances", "records:correspondent"),
    ("03", "Instant native SETTLED with beneficiary delivery", "records:instant"),
    ("04", "Source-final/destination-pending obligation13300", "pending_before_restart"),
    ("05", "Restore and native recovery under original terms", "final_after_restoration"),
    (
        "06",
        "Repeated recovery/retry leaves one credit and2 payment groups",
        "records:lost_ack_duplicate and final_after_restoration",
    ),
    (
        "07",
        "Source shortage rejected before financial effect",
        "records:shortage; opening/final positions identical",
    ),
    (
        "08",
        "Separately invoked read-only SQL financial auditor passes",
        "independent_worker_SQL_audit fields",
    ),
    ("09", "Native canonical durable replay passes", "replay fields"),
    (
        "10",
        "Database+application recreated without deleting volumes; exact pending financial/session state survives",
        "persisted_after_restart equals pending_before_restart",
    ),
]
table = (
    "| Case | Result | Native evidence | Location in container_smoke.json |\n|---|---|---|---|\n"
    + "\n".join(
        f"| CT-{number} | PASS | {description} | {location} |" for number, description, location in ct
    )
)
common = f"\n\nCurrent isolated-install regression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds. One existing Starlette/AnyIO deprecation warning. Source revision is SHA256_MANIFEST.json in review ZIP; no invented Git revision. Financial source bytes match R4 except the nonfinancial operator-ingress adapter. Original10000-in-60 target remains UNVERIFIED; no throughput campaign rerun. No external publication.\n"
reports = {
    "CLEAN_INSTALL": """CLEAN FULL-STACK INSTALLATION: PASS on the tested Windows host.

Created .work/r5/source from source/configuration only: initial manifest proves no .venv or node_modules. Installed38 locked backend packages into a new uv virtual environment and26 frontend packages into a new pnpm tree. Download caches reused; installation directories and system site-packages not reused. Exact lock hashes and installed versions in environment.json. Local runtime Python3.12.10, uv0.12.5, Node24.22.1, pnpm11.25.0, PostgreSQL18.6. Source app/tests/migrations/scenarios/frontend source bytes match the release tree.

Created uniquely scoped database from context.json, verified zero public tables before migrations, applied001–007 and loaded19 immutable synthetic fixtures. Did not use a preconfigured development database as clean-install proof. Existing PostgreSQL server binary/service was a prerequisite; not claimed newly installed. Fresh production frontend built with TypeScript/Vite and was served by the new backend from this copied checkout. Health, browser connectivity, all three rails, EUR route, expiry/shortage rejection, independent SQL audit, replay and backend restart proof pass. Existing shared PostgreSQL service was not restarted locally. Local restart_scope is backend, database_restart_verified is false by design.

Browser ran from freshly installed dependencies against the isolated service:15 acceptance behaviors grouped into10 workflows, zero page errors, expected409 negative-control console record retained. Existing Chromium binary reused, not claimed freshly downloaded. Build retains the known large-chunk warning. Exact command sequence, ports, creation proof, hashes and evidence are in r5_evidence/installation.json, context.json, local_database.json, environment.json, local_browser.json and sanitized logs. Generic reproducible commands and prerequisites are in README.
""",
    "DOCKER_DIAGNOSTICS": """DOCKER ENGINE ACCESS: PASS in approved host execution context.

Docker CLI29.9.2, engine29.9.2, Compose5.6.1, desktop-linux context. Initial restricted command received the exact pipe permission error recorded in docker_diagnostics.json. Normal approved host access succeeded; Docker Desktop/backend processes were running. No daemon ACLs, user groups, security settings or unrelated services changed. This is a resolved execution-context restriction, not an unavailable engine. No current user action required for the tested host.

Actual deployment issues found and fixed: .dockerignore now recursively excludes source bytecode, extra virtual environments and .work caches; final image source-bytecode count0. Backend health check and persistent evidence volume added. uv dependency downloads are bounded with30-second request timeout/no retries; build driver has300-second overall limit. Frozen dependency pins and pinned uv0.9.8 integration preserved. Final container Python3.12.15 with compiled frontend from Node22 stage. Image ID and actual source hashes in environment.json.

Docker loopback publishing arrives at Uvicorn from the container's default bridge gateway, initially producing LOCAL_OPERATOR_ONLY409. Explicit Compose opt-in now trusts only that exact kernel gateway with a loopback Host; other peers/hosts and external origins remain rejected. No wildcard trust, forwarded-header authorization or arbitrary private-subnet allowance. Security regression and real host-browser acceptance both pass. Synthetic network remains host-loopback-only; database not published.
""",
    "CONTAINER_SMOKE": table
    + "\n\nPASS on the final verified runtime image. R5 Compose project/volumes were generated independently of existing deployments. Built actual frontend/backend stages, waited for Postgres/application health, applied migration007, loaded fixtures, served real frontend and ran native worker through HTTP plus separate direct SQL verification. The production image has no dev pytest requirement; worker uses installed runtime dependencies. Additional EUR/expiry/locked-quote checks pass. Exact commands/return codes in docker_commands.json and logs; final container_smoke.json is RESTART_PASS with restart_scope stack and database_restart_verified true. A full host-browser matrix also passed against the image; static frontend bytes equal the isolated Windows build. Services were stopped afterward; scoped ledger/evidence volumes retained for review. No unrelated containers stopped and no volumes deleted.",
    "QUALITY_REPORT": f"""MAINTAINED QUALITY GATES: PASS.

Full Python suite executed with the fresh environment, copied source and new R5 database: {suite["tests"]} passed, zero failures/errors/skips. One new regression covers explicit bridge trust, external-origin rejection, other-client rejection and non-loopback Host rejection. Existing G11/G12/G14 behavior retained; financial implementation bytes were not changed. Fresh frontend TypeScript/production build passed, and established real browser checks passed both locally and against Docker.

Ruff lint and formatting cover all application/tests/experiments/scripts source. Maintained mypy scope includes application, all tests, runtime chaos/crash helpers, R4 verification helpers, fixture loader and active R5 release utilities; exact list and command in quality.json. No broad suppressions added. The all-source inventory still reports {quality["legacy_error_count"]} errors in {len(quality["legacy_error_files"])} explicitly identified historical experiment/regeneration scripts. The earlier three maintained test errors were already repaired; two new helper-only variable typing errors encountered during R5 were repaired. No active maintained error remains.

Legacy files: {", ".join(quality["legacy_error_files"])}. These are closed-milestone generation/forensics utilities, not current application or maintained acceptance entry points. Their errors are disclosed, not claimed passing. Prior ZIPs/frozen source under docs are immutable evidence. Virtual environments, node_modules, dist and caches are generated outputs. Do not describe the whole repository as universally type-clean. Full inventory, exclusions and passing supported gate are preserved in quality.json/logs.
""",
    "FINANCIAL_SMOKE": """FINANCIAL SMOKE: PASS in fresh local installation and Linux container.

Worker executes actual API actions against actual PostgreSQL, not mocked responses. Healthy atomic, correspondent and instant settlements; routed EUR proceeds; quote expiry; insufficient funding; lost acknowledgment/repeated retry; source-final pending obligation; restoration and restart recovery are asserted. Native reconciliation and durable replay run separately from the SQL auditor. The worker invokes independent_audit directly with its own Store connection; it does not merely trust the API's PASS label.

Pending state is exactly preserved across restart before recovery. Beneficiary remains13300SGD minor units after repeated recovery and exactly two instant payment journal groups exist. Rejected shortage state leaves opening positions unchanged. Quote integrity representative honors accepted version1 after version2 is introduced; stored quote and final booked terms match. No financial engine, fee, FX commitment, posting semantics, policy or finality rule changed for deployment. Every application source file except the ingress guard matches R4 bytes; Docker application/migration/scenario bytes and fresh installation source hashes match the release tree. Shared assumptions remain schema and documented synthetic contracts; this is not external banking certification.

Evidence: local_smoke.json/container_smoke.json include opening, intermediate, final/restart states, native audit, independent worker SQL audit and replay hashes. All current261 tests preserve previously verified corruption and process-crash controls. No large chaos or performance campaign repeated.
""",
    "RELEASE_ENVIRONMENT": """RELEASE-ENVIRONMENT READINESS: GO for the declared local synthetic research preview.

Both supported paths demonstrated: isolated Windows application installation on an existing supported PostgreSQL host, and Linux Docker application/PostgreSQL stack. Local build and container frontend bytes match. Database and evidence survive scoped service recreation. Application/DB health, frontend startup, API/native financial execution, independent audit and replay verified. No remaining environmental blocker in this tested setup. Verification services stopped cleanly; scoped synthetic data retained. Existing user data and other deployments untouched.

Runtime caveats: supplied Docker network uses trust authentication and host-loopback publishing; not production authentication or public financial hosting. Browser binaries/OS/database service on Windows are prerequisites; caches reused only as downloads. Image runtime Python3.12.15 versus local3.12.10 both tested. Node/Python base tags can resolve newer patch images on a future rebuild; observed tested image ID is recorded, not a promise of bit-reproducible future upstream tags. The uv binary digest and application lockfiles remain pinned. Frontend large-chunk warning remains.

Supported scope: five modeled banks, four currency definitions, four atomic routed corridors, modeled correspondent USD paths, USD/SGD instant link. Not all rail/currency combinations are supported. Live faults are bounded destination outage/lost ACK; recorded playback separate. R4 throughput evidence remains native Windows1000 in14.999s; not a container benchmark. Original10000-in-60 remains UNVERIFIED, so original full-vision completion is not claimed.

License remains all-rights-reserved with no selected public software license. This review package grants no redistribution/publication permission. External GitHub publication was not performed and remains owner-gated. Freeze the verified synthetic preview for owner review rather than reopen completed financial/UI work.
""",
    "FINAL_GO_NO_GO": """CLEAN FULL-STACK INSTALLATION: PASS.

DOCKER PAYMENT SMOKE: PASS — CT-01 through CT-10 executed.

MAINTAINED QUALITY GATES: PASS within the explicit supported scope; legacy inventory disclosed.

RELEASE-ENVIRONMENT READINESS: GO for tested local synthetic research-preview deployment.

Windows isolated source/dependencies/database and Linux containers passed native payments, health, frontend/browser, quote/funding/finality, independent audit, durable replay and restart checks. Container services/database were recreated without deleting new volumes; local test restarted only its own backend. No remaining environment blocker on the tested host. New ingress regression and complete fresh-install suite pass. No financial implementation changes, original gate redefinition, external publication or throughput rerun.

Original10000-payments-within60-seconds target remains UNVERIFIED. Public release/license selection requires owner action; current LICENSE reserves rights. Source/operating limitations in README and companion reports remain explicit. Recommended action: freeze this verified local preview and review the source/evidence package; treat higher-throughput ambitions and any external publication separately.

Reproduce the exact R5 setup using scripts/r5_prepare.py, scripts/r5_local.py and scripts/r5_docker.py on a new copy, or use the standard local/Docker commands in README. Generated context selects unique project/database names and free loopback ports. Helpers refuse to reuse an existing local installation database. scripts/r5_quality.py supplies the maintained gate and disclosed inventory; scripts/r5_metadata.py verifies source/image/lock identities. Driver command logs and images/volume ownership are included. Do not overwrite an existing review archive.
""",
}
common = f"\n\nVerification on client date2026-10-31 (Asia/Calcutta); raw operational timestamps retain their recorded UTC offsets. Full isolated regression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds. Exact source revision: SHA256_MANIFEST.json in archive; no invented Git commit.\n"
for name, body in reports.items():
    (DOCS / f"THUNDERBOLT_R5_{name}.md").write_text(
        "# THUNDERBOLT-R5 " + name.replace("_", " ") + "\n\n" + body + common, encoding="utf-8"
    )

files = []
for folder in ("railstorm", "tests", "migrations", "scenarios", "scripts", "experiments", "frontend/src"):
    files += [
        p
        for p in (ROOT / folder).rglob("*")
        if p.is_file()
        and p.suffix not in (".pyc", ".pyo", ".log")
        and not {"__pycache__", "reports", "work", "node_modules"}.intersection(p.parts)
    ]
files += [
    ROOT / name
    for name in (
        "README.md",
        "LICENSE",
        ".dockerignore",
        "Dockerfile",
        "docker-compose.yml",
        "pyproject.toml",
        "uv.lock",
        "alembic.ini",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/pnpm-workspace.yaml",
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "frontend/r3.e2e.mjs",
    )
]
files += list(DOCS.glob("THUNDERBOLT_R5_*")) + [p for p in OUT.rglob("*") if p.is_file()]
files += [DOCS / "IMPLEMENTATION_STATUS.md", DOCS / "FINAL_prior_archives.json"]
for prefix in ("THUNDERBOLT_R2_1_", "THUNDERBOLT_R3_", "THUNDERBOLT_R4_"):
    files += list(DOCS.glob(prefix + "*.md"))
files += [p for p in (DOCS / "r4_evidence").iterdir() if p.is_file() and p.suffix in (".py", ".json")]
contents = {}
for path in sorted(set(files)):
    if not path.is_file():
        continue
    data = path.read_bytes()
    if path.suffix == ".xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if re.search(rb"[A-Za-z]:[\\/]+Users[\\/]+", data, re.IGNORECASE) or any(
        pattern in data for pattern in (b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
    ):
        raise RuntimeError("PRIVATE_HOST_DATA: " + path.relative_to(ROOT).as_posix())
    contents[path.relative_to(ROOT).as_posix()] = data
fresh = ROOT / ".work/r5/source"
for path in (fresh / "frontend/dist").rglob("*"):
    if path.is_file():
        contents["frontend/dist/" + path.relative_to(fresh / "frontend/dist").as_posix()] = path.read_bytes()
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
target = ROOT.parent / "RAILSTORM_THUNDERBOLT_R5_REVIEW.zip"
if target.exists():
    raise FileExistsError(target)
with ZipFile(target, "w", ZIP_DEFLATED) as zipped:
    for name, data in contents.items():
        zipped.writestr("railstorm/" + name, data)
    zipped.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(manifest, indent=2))
with ZipFile(target) as zipped:
    assert zipped.testzip() is None
    assert all("docs/THUNDERBOLT_R5_" + name + ".md" in manifest for name in reports)
    for name, sha in manifest.items():
        assert hashlib.sha256(zipped.read("railstorm/" + name)).hexdigest() == sha
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
