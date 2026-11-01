"""Publish local R2.1 evidence only after successful regression and hash checks."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
suite = ET.parse(docs / "THUNDERBOLT_R2_1_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
prior = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
all_prior = json.loads((docs / "THUNDERBOLT_R2_1_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in all_prior:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
common = f"\n\nCurrent regression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds. Source identity: SHA256_MANIFEST.json inside review ZIP. No schema change; existing fresh-db crash tests migrate to007. No frontend, Docker, optimizer or throughput work in this scope.\n"
reports = {
    "CONCURRENT_FX": """CQ-01–CQ-06: PASS for the tested native contract.

`market_quotes.replace` appends MARKET_QUOTE events under the same PostgreSQL run-row lock used by financial commands. Versions strictly increase relative to introduced runtime and effective scheduled versions. No old quote is overwritten. Validity starts at the update transaction's logical time, expiry is exclusive. Runtime quote is selected for new preparations; an accepted prepared quote remains binding until its own expiry. Atomic source/destination financial commit is one transaction.

`test_quote_update_execution_race[False/True]` holds the run lock in a third session, observes one then two independent sessions waiting in pg_stat_activity, releases the gate and checks both orderings. Execution-first uses version1/output13300; update-first uses version2/output14000. A later version3 leaves committed payment terms unchanged. Evidence CQ-order-False/True.json includes opening, before-later-update, final snapshots, exact journals, quote fields, events, audits/replay and observed waiters.

`test_route_quote_and_fee_change_requires_new_decision` verifies stale persisted routing decision rejection after both rate/proceeds and fee change, then a new persisted decision settles version2/fee30/output14000. Atomic router performs its final market identity/fee validation, preparation and settlement under one rail-world lock, closing the preflight/preparation race. A durable routing binding may survive a rejected execution; it cannot silently reprice or fall back.

Reproduce: `python -m pytest -q tests/test_r21_finance.py -k 'quote_update or route_quote'`. Runtime update is a local Python operator API; no public market endpoint was added. This finite lock-scheduled matrix is not a proof of all distributed schedules.
""",
    "SOURCE_FINAL_FX": """SF-01–SF-08: PASS for supported USD/SGD instant link.

`test_source_final_obligation_keeps_original_quote` creates version1, prepares principal10000 with source fee25, commits the source stage and leaves destination pending with funded obligation13300. A version2 market quote at31 changes prospective rate to140/100. Old obligation and locked version1 quote remain byte-for-byte unchanged. Reusing the old payment ID with changed principal is rejected IDEMPOTENCY_KEY_REUSED; retrying the original command returns historical terms. Reconstructed InstantLink component recovers twice; beneficiary receives exactly13300, exactly two payment journal groups remain and old version1 is retained. Evidence SF-01-08.json contains opening, pending, post-update and final financial state and full event histories. Every state passes native reconciliation, independent SQL audit and durable replay.

New native instant preparations may use current market rates but retain the existing fixed25 source fee, domestic amount/cap checks and USD/SGD-only participant contract. Shared-router instant execution rejects dynamic-market worlds because their new terms are not advertised by that adapter; a dedicated regression verifies rejection before financial mutation. Existing fixed-term instant routing remains supported. There is no automatic compensation, source reversal or amendment of a locked payment quote. Reproduce: `python -m pytest -q tests/test_r21_finance.py -k source_final`.
""",
    "SHORTAGE_EVIDENCE": """LS-01–LS-05: PASS for the two tested native mechanisms.

`test_correspondent_shortage_restore_preserves_prior_hop` makes existing funded SR_USD liquidity unavailable through a recorded account buffer, creates a native chain, advances in50ms steps and captures each state. First-hop postings persist while onward state reports INSUFFICIENT_CORRESPONDENT_LIQUIDITY. Removing the recorded buffer restores availability; subsequent native advances settle. Every prior payment journal is compared exactly against final history. LS-correspondent.json contains opening state, all steps/events, pending obligations, reservation state and final audit/replay. This models an operational liquidity restriction and restoration, not external money creation.

`test_destination_shortage_then_funded_new_attempt` begins with SGD provider funding100, rejects a10000USD intent before source finality, verifies zero beneficiary credit, then explicitly injects20000SGD synthetic funding with a balanced OPENING offset journal. The rejected intent remains terminal; a new intent prepares, becomes source-final/destination-pending, then recovers twice to one13300SGD credit. LS-destination.json distinguishes opening, rejected-before-source, pending-after-source and final states. No absent financial stage is fabricated. Native commands define route; these tests do not create shared-router records. Quote version is absent on legacy fixed quote where the engine does not assign one; no fictional version is substituted.

Reproduce: `python -m pytest -q tests/test_r21_finance.py -k 'shortage or destination'`.
""",
    "EVENT_INTEGRITY": """ET-01–ET-06: PASS for anchored copied exports.

`test_export_tamper_matrix` executes a native atomic payment and exports actual stored events. An ordinary stored UPDATE is rejected APPEND_ONLY. `event_integrity.verify_export` independently recomputes canonical hashes, previous-hash links, contiguous sequences, schema and aggregate versions, and checks externally supplied expected event count and final chain head. Unmodified export passes; copied financial delta edit, middle deletion, tail truncation and forged finality all fail. Original stored events are compared exactly with the original export and native replay still passes. No trigger is disabled. Evidence ET-01-06.json includes original events, anchor and each negative result.

Limits: this is integrity relative to a trusted retained count/head, not a signature, identity authentication or proof against an attacker replacing the export and trusted anchor together. A rewritten/rehashed history cannot match a separately retained genuine head without breaking the hash assumption; no stronger cryptographic claim is made. Reproduce: `python -m pytest -q tests/test_r21_integrity.py`.
""",
    "TEST_REPORT": """Eight new tests (two race orderings, routed replacement, source-final obligation, explicit unsupported dynamic-instant-routing rejection, two shortage paths, anchored export controls). Initial targeted suite7 passed. Maintained lint/format and type gates cover railstorm/tests. Full current-source regression recorded in THUNDERBOLT_R2_1_pytest.xml. Two earlier regression runs were deliberately interrupted as the router preflight/preparation race and unsupported dynamic-instant-routing boundary were tightened; interrupted output is not passing evidence. No frontend/API request schemas changed; existing API regression tests remain included.

Commands: `python -m pytest -q tests/test_r21_finance.py tests/test_r21_integrity.py --junitxml=docs/THUNDERBOLT_R2_1_targeted.xml`; `python -m pytest -q --junitxml=docs/THUNDERBOLT_R2_1_pytest.xml`; `ruff check railstorm tests`; `ruff format --check railstorm tests`; `mypy --explicit-package-bases railstorm tests`.
""",
    "FINAL_GO_NO_GO": """G11 — FX ACCEPTANCE: PASS for the documented native atomic/instant quote contract and finite acceptance matrix.

G12 — ADVERSARIAL ACCEPTANCE: PASS for the documented finite native scenario matrix.

This closes R2's stated quote-update, source-final obligation, shortage stage-evidence and copied-event integrity gaps. Prior R2 tests and actual crash/contention controls remain in the current regression. Financial audit and replay are checked at new clean intermediate/final states, and negative exports fail as required. No material financial safety failure observed. Every CQ/SF/LS/ET requirement is indexed in the five companion reports; full event and financial evidence is included.

These gate verdicts do not establish universal concurrency correctness, power-loss durability, production readiness, live market connectivity, browser completion, Docker readiness or throughput targets. Next acceptance workstream is live Crisis Lab and complete browser/fresh frontend acceptance, with performance kept separately gated. All thirteen original archive hashes are rechecked before packaging, and all existing archives are retained.
""",
}
for name, body in reports.items():
    (docs / f"THUNDERBOLT_R2_1_{name}.md").write_text(
        "# THUNDERBOLT-R2.1 " + name.replace("_", " ") + "\n\n" + body + common
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
files += (
    list(docs.glob("THUNDERBOLT_R2_1_*"))
    + list((docs / "r21_evidence").glob("*.json"))
    + list((docs / "thunderbolt_r2_evidence").glob("*.json"))
)
files += [
    docs / "FINAL_prior_archives.json",
    docs / "IMPLEMENTATION_STATUS.md",
    docs / "THUNDERBOLT_R2_FX_MATRIX.md",
    docs / "THUNDERBOLT_R2_ADVERSARIAL_MATRIX.md",
]
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
target = root.parent / "RAILSTORM_THUNDERBOLT_R2_1_REVIEW.zip"
if target.exists():
    raise FileExistsError(target)
with ZipFile(target, "w", ZIP_DEFLATED) as z:
    for name, data in contents.items():
        z.writestr("railstorm/" + name, data)
    z.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(manifest, indent=2))
with ZipFile(target) as z:
    assert z.testzip() is None
    assert all("docs/THUNDERBOLT_R2_1_" + name + ".md" in manifest for name in reports)
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
