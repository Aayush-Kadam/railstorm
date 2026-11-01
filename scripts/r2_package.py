"""Generate gate evidence indexes and a new verified R2 archive."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
suite = ET.parse(docs / "THUNDERBOLT_R2_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
prior = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
fx = [
    ("01", "PASS", "Native scheduled version1 settlement; four currency pairs; test_r2_fx"),
    ("02", "PASS", "test_final_fx expiration; test_r2_fx expired prepared reservation release"),
    ("03", "PASS", "Old payment/quote fields remain identical after version2 execution"),
    ("04", "PASS", "test_thunderbolt_routing rejects changed version before binding"),
    ("05", "PASS", "New decision/version executes; shortage rejects after expired prior preparation"),
    (
        "06",
        "PARTIAL",
        "Instant source finality retained in message/crash cases; no quote-update API on instant rail",
    ),
    (
        "07",
        "PARTIAL",
        "Pending amount13300 retained through restart; update-against-pending operation not implemented",
    ),
    (
        "08",
        "PASS",
        "Atomic component reconstruction settles persisted historical quote; instant crash recovery retains13300",
    ),
    ("09", "PASS", "Stale route decision rejected; expired prepared payment cannot commit"),
    ("10", "PASS", "FX-shortage-final.json: increased output rejected, no active holds or payment journals"),
    ("11", "PASS", "Amounts3/101/10001, floor integer conversion across four pairs"),
    ("12", "PASS", "Four source fee postings of25 exactly total100 per currency-specific world"),
    (
        "13",
        "PARTIAL",
        "Concurrent executions use immutable preloaded version; competing quote-update command absent",
    ),
    ("14", "PASS", "Replay full history with both versions; old payment projections unchanged"),
    ("15", "PASS", "test_thunderbolt_routing unsupported corridor produces no executable route"),
]
adv = [
    (f"{i:02d}", "PASS", f"test_r2_messages ADV-{i:02d}; opening/intermediate/final evidence")
    for i in range(1, 8)
]
adv += [
    ("08", "PASS", "Existing real two-session lock contention test retained in regression"),
    (
        "09",
        "PARTIAL",
        "Existing correspondent shortage regression passes; new complete stage evidence not captured",
    ),
    ("10", "PARTIAL", "Existing IL-03 prefunding test retained; new full stage/recovery record absent"),
    ("11", "PASS", "Real contention test plus concurrent version2 execution: one winner, one rejection"),
    ("12", "PASS", "FX-shortage-prepared/final evidence"),
    (
        "13",
        "PASS",
        "Actual worker exit74 before destination posting; durable source-final obligation survives",
    ),
    ("14", "PASS", "Actual worker exit73 after destination commit/LOST ACK"),
    ("15", "PASS", "Exit73 occurs inside native recovery delivery override"),
    ("16", "PASS", "Two newly constructed recovery invocations in isolated crash case"),
    ("17", "PASS", "Exit74 preserves source-final pending obligation13300"),
    ("18", "PASS", "Repeated recovery/message cases retain two payment journals and one credit"),
    ("19", "PASS", "Actual +1 projection corruption detected; restored then recovered and audited"),
    ("20", "PASS", "False SETTLED label detected independently and by replay/native audit"),
    (
        "21",
        "PARTIAL",
        "Database APPEND_ONLY rejects mutation; persisted-tamper detection test not newly injected",
    ),
    ("22", "PASS", "Actual duplicate journal identity rejected by DB; clean pending state preserved"),
]


def table(prefix, rows):
    return "| ID | Result | Evidence / scope |\n|---|---|---|\n" + "\n".join(
        f"| {prefix}-{i} | {status} | {evidence} |" for i, status, evidence in rows
    )


common = f"\n\nFull current regression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds. New tests:19 (six FX, seven message, two process-boundary, four corruption/duplicate controls). No ledger/routing economics changes; optional crash-harness boundary/output parameters added. Fixed-order unchanged.\n"
reports = {
    "FX_MATRIX": table("FX", fx)
    + "\n\nContract: atomic quote versions are predeclared immutable nonoverlapping schedules. New intent must use currently valid quote; changed routed terms require new persisted decision. No public concurrent market-update command exists. Instant rail uses fixed133/100; mutating a source-final obligation is unsupported. Do not treat this as full dynamic repricing support.",
    "ADVERSARIAL_MATRIX": table("ADV", adv)
    + "\n\nExit74 is before destination recovery financial action, after authoritative pending-state inspection. Exit73 is inside recovery after a durable destination posting and LOST acknowledgment, before recovery event. Neither is a machine power-loss simulation. Append-only event mutation is prevented, not presented as detected post-storage tampering.",
    "FINANCIAL_AUDIT": "Every successful newly captured native state calls independent_audit, native reconcile and replay_check. FX and message evidence includes authoritative snapshots/inspection. Negative projection and false-finality controls require independent rejection, native financial errors and replay mismatch; restored states pass. Duplicate journal identity and event update are rejected by PostgreSQL. No balancing repair is fabricated; only exact controlled corruption is reversed in uniquely named disposable worlds. Crash databases are separately created/migrated/dropped.",
    "REPLAY_REPORT": "Native replay is asserted after each captured FX/message stage, crash interruption and recovery, and after negative-control restoration. Historical payment data is compared exactly after version2 exists. Global full-history event hash grows when later events append; equality of whole-history hashes before/after is not expected. APPEND_ONLY rejection preserves the original event chain; stored corruption detection is not newly demonstrated by that prevention test.",
    "TEST_REPORT": "Targeted runs: messages/crashes9 passed; FX6 passed after fixing test TIME_REVERSAL scheduling; controls4 passed after recognizing APPEND_ONLY as prevention. Maintained type command: mypy --explicit-package-bases railstorm tests experiments/p2ar_crash.py experiments/r2_crash_worker.py. Ruff check/format across maintained source/tests. Schema unchanged; two new process-boundary tests migrate fresh disposable databases to007. Reproduce: python -m pytest -q tests/test_r2_fx.py tests/test_r2_messages.py tests/test_r2_crashes.py tests/test_r2_controls.py. Full command: python -m pytest -q --junitxml=docs/THUNDERBOLT_R2_pytest.xml. No frontend/API contract changed; existing API checks retained.",
    "FINAL_GO_NO_GO": "G11 — FX ACCEPTANCE: PARTIAL. G12 — ADVERSARIAL ACCEPTANCE: PARTIAL. No new financial safety failure observed. Exact unimplemented capability: a validated concurrent quote-update command in railstorm/ledger.py and source-final instant quote-version update/rejection contract in railstorm/rails/instant_link.py. Existing quote_schedule is supplied at world initialization; it is not a runtime update service. Reproduce supported boundaries with tests/test_r2_fx.py and tests/test_thunderbolt_routing.py. Required remedy is an immutable append-only quote registry/update API with explicit per-rail eligibility/finality contracts and true updater/executor concurrency tests. Complete ADV09/10 stage evidence and stored-tamper verification in a disposable database before G12 PASS. These are remaining implementation/evidence gaps, not environmental hard blockers. Next acceptance workstream: finish those exact G11/G12 gaps; browser/Docker/performance remain separate. No full original-vision completion claim.",
}
for name, body in reports.items():
    (docs / f"THUNDERBOLT_R2_{name}.md").write_text(
        f"# THUNDERBOLT-R2 {name.replace('_', ' ')}\n\n" + body + common
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
        "frontend/index.html",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
    )
]
files += list(docs.glob("THUNDERBOLT_R2_*")) + list((docs / "thunderbolt_r2_evidence").glob("*.json"))
files += [docs / "FINAL_prior_archives.json"]
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
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
target = root.parent / "RAILSTORM_THUNDERBOLT_R2_REVIEW.zip"
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
            "historical_archives_verified": len(prior),
        }
    )
)
