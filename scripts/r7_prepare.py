"""Preserve the actual working source and freeze the historical acceptance contract."""

import hashlib
import json
import uuid
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from experiments.r4_profile import resources
from railstorm.domain import PaymentCommand, digest
from railstorm.ledger import INITIAL
from railstorm.scenarios import Scenario
from railstorm.simulation import compile_queue

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"
WORK = ROOT / ".work/r7"
R6_HASH = "e9b63d2c739527aecdb74acc4dd7327c331f84c606bd5ec591f83c6332a630f9"


def main():
    OUT.mkdir(exist_ok=False)
    WORK.mkdir(exist_ok=False)
    source_files = []
    for folder in ("railstorm", "tests", "experiments", "scripts", "migrations", "scenarios", "frontend/src"):
        source_files += [
            p
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and not {"__pycache__", "reports", "node_modules"}.intersection(p.parts)
        ]
    source_files += [
        ROOT / name
        for name in (
            "README.md",
            "LICENSE",
            "pyproject.toml",
            "uv.lock",
            "alembic.ini",
            "Dockerfile",
            ".dockerignore",
            "docker-compose.yml",
            "frontend/package.json",
            "frontend/pnpm-lock.yaml",
        )
    ]
    revision = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files
    }
    prior = [
        {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(ROOT.parent.glob("*.zip"))
    ]
    assert (
        hashlib.sha256((ROOT.parent / "RAILSTORM_THUNDERBOLT_R6_REVIEW.zip").read_bytes()).hexdigest()
        == R6_HASH
    )
    with ZipFile(ROOT.parent / "RAILSTORM_THUNDERBOLT_R6_REVIEW.zip") as archived:
        assert archived.testzip() is None
        manifest = json.loads(archived.read("railstorm/SHA256_MANIFEST.json"))
        differences = [name for name, sha in revision.items() if name in manifest and sha != manifest[name]]
        for name, sha in manifest.items():
            if name.startswith(("railstorm/", "migrations/", "scenarios/")):
                data = archived.read("railstorm/" + name)
                assert hashlib.sha256(data).hexdigest() == sha
                target = WORK / "baseline" / name
                assert target.resolve().is_relative_to((WORK / "baseline").resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    with ZipFile(WORK / "prechange_source.zip", "w", ZIP_DEFLATED) as checkpoint:
        for path in source_files:
            checkpoint.write(path, path.relative_to(ROOT).as_posix())
    scenario = Scenario.model_validate_json(
        (ROOT / "scenarios/stress_10000.json").read_text(encoding="utf-8")
    )
    contract = {
        "authority": "Master prompt section21 engineering target; historical p01_forensics/run_scenario and R4/R6 frozen contract",
        "primary": "ATOMIC_PVP USD/SGD; one Atlas source, one Meridian destination, one Sterling FX/liquidity provider",
        "institutions_initialized": 5,
        "funding": INITIAL,
        "independent_intents": 10000,
        "scenario": scenario.model_dump(),
        "command_defaults": PaymentCommand(idempotency_key="example", principal_minor=10000).model_dump(
            mode="json"
        ),
        "assignments": {
            str(n): digest(compile_queue(scenario.model_copy(update={"payment_count": n})))
            for n in (100, 500, 1000, 2500, 5000, 10000)
        },
        "required_effects": {
            "payments": 10000,
            "quotes": 10000,
            "decisions_commit": 10000,
            "consumed_reservations": 20000,
            "journal_entries_including_opening": 20004,
            "journal_lines_including_opening": 50008,
            "events_and_matching_outbox": 140034,
        },
        "timing": "perf_counter before create_run through return of final step1000, including connection setup in commands, initialization, queue generation/persistence, native ARRIVAL/SETTLE/RECOVER/EXPIRY, hashes, all financial writes and durable commit; retrospective independent audit/reconcile/replay separately timed as historical harness",
        "api_overhead": "Direct native historical Python harness, not HTTP/WebSocket/browser overhead",
        "completion": "Fresh independent database read after final committed step: SETTLED and ACKNOWLEDGED; required quotes/decisions/reservations/journals/events/outbox durable, no pending or duplicate credit; audit and replay pass",
        "transactions": "Preserve historical initialization and <=1000 virtual-event batch commits; do not enlarge batch or alter independently visible command commit semantics",
        "allowed_concurrency": "Primary single historical deterministic queue worker; diagnostic workers separate, never substitute unlike worlds or reorder chain for a target claim",
        "durability": "PostgreSQL fsync=on synchronous_commit=on; triggers/constraints/hash/replay unchanged",
        "ambiguity": "Master says about60 seconds as target, not precise original transaction batching. Existing verified native harness resolves timing/step boundaries; R7 fixes hard<=60 seconds and preserves its exact batching. No server/API-overhead or retrospective audit work silently removed.",
    }
    context = {
        "database": "railstorm_r7_" + uuid.uuid4().hex[:8],
        "revision": revision,
        "prior_archives": prior,
        "differences_from_R6_archive": differences,
        "checkpoint_sha256": hashlib.sha256((WORK / "prechange_source.zip").read_bytes()).hexdigest(),
        "git": "No .git repository at project root; actual source hash manifest and immutable checkpoint used",
        "resources_at_freeze": resources(),
    }
    (OUT / "baseline.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
    (OUT / "contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "source_files": len(revision),
                "prior_archives": len(prior),
                "user_changes_preserved": differences,
                "resources": resources(),
            }
        )
    )


if __name__ == "__main__":
    main()
