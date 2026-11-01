"""Create an evidence-checked R4 performance review archive without overwrites."""

import hashlib
import importlib.metadata
import json
import re
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
evidence = docs / "r4_evidence"


def read(name):
    return json.loads((evidence / name).read_text(encoding="utf-8"))


suite = ET.parse(docs / "THUNDERBOLT_R4_pytest.xml").getroot()[0].attrib
assert suite["failures"] == suite["errors"] == suite["skipped"] == "0"
mixed = read("matched_mixed.json")
atomic = read("matched_atomic_500.json")
hash_only = read("matched_hash_only.json")


def median(rows, label, key, label_key="label"):
    return statistics.median(key(r) for r in rows if r[label_key] == label)


old_m = median(mixed, "baseline", lambda r: r["result"]["timing"]["wall_seconds"])
new_m = median(mixed, "modified", lambda r: r["result"]["timing"]["wall_seconds"])
old_a = median(atomic, "baseline", lambda r: r["execution_seconds"], "implementation")
new_a = median(atomic, "modified", lambda r: r["execution_seconds"], "implementation")
assert new_m < old_m and new_a < old_a
assert max(r["result"]["timing"]["wall_seconds"] for r in mixed if r["label"] == "modified") < min(
    r["result"]["timing"]["wall_seconds"] for r in mixed if r["label"] == "baseline"
)
assert max(r["execution_seconds"] for r in atomic if r["implementation"] == "modified") < min(
    r["execution_seconds"] for r in atomic if r["implementation"] == "baseline"
)
assert len({r["result"]["canonical_result_hash"] for r in mixed}) == 1
assert len({r["state_hash"] for r in atomic}) == len({r["replay"]["event_hash"] for r in atomic}) == 1
assert len({r["execution_sql"]["logical_executions"] for r in atomic}) == 1
for row in mixed:
    for report in row["differential"].values():
        assert report["old"] == report["new"] and report["independent"]["pass"]
for row in atomic:
    assert (
        row["baseline_audit_identical"]
        and row["native"]["financial_errors"] == 0
        and row["replay"]["match"]
        and row["independent"]["pass"]
    )
prior = json.loads((docs / "THUNDERBOLT_R4_prior_archives.json").read_text(encoding="utf-8-sig"))
original = json.loads((docs / "FINAL_prior_archives.json").read_text(encoding="utf-8-sig"))
for row in prior + original:
    assert hashlib.sha256((root.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
contract = read("contract.json")
assert (
    hashlib.sha256((evidence / "baseline_audit.py").read_bytes()).hexdigest()
    == contract["baseline_audit_sha256"]
)
with ZipFile(root.parent / "RAILSTORM_THUNDERBOLT_R3_REVIEW.zip") as old_archive:
    assert old_archive.read("railstorm/railstorm/audit.py") == (evidence / "baseline_audit.py").read_bytes()
    assert old_archive.read("railstorm/railstorm/store.py") == (evidence / "baseline_store.py").read_bytes()
implementation = {
    "baseline_sources": {
        name: hashlib.sha256((evidence / ("baseline_" + name)).read_bytes()).hexdigest()
        for name in ("audit.py", "store.py")
    },
    "modified_sources": {
        name: hashlib.sha256((root / "railstorm" / name).read_bytes()).hexdigest()
        for name in ("audit.py", "store.py")
    },
    "baseline_matches_prior_R3_archive": True,
    "dependencies": {
        name: importlib.metadata.version(name) for name in ("SQLAlchemy", "psycopg", "pydantic")
    },
    "mixed_median_before": old_m,
    "mixed_median_after": new_m,
    "mixed_reduction_percent": 100 * (old_m - new_m) / old_m,
    "atomic500_median_before": old_a,
    "atomic500_median_after": new_a,
    "atomic500_reduction_percent": 100 * (old_a - new_a) / old_a,
}
(evidence / "implementation_manifest.json").write_text(json.dumps(implementation, indent=2))
ladder = [read(f"atomic-{count}-modified.json") for count in (25, 100, 500, 1000)]
table = (
    "| Tier | Completed | Execution s | Payments/s | Audit s | Replay s | Peak client MiB |\n|---|---:|---:|---:|---:|---:|---:|\n"
    + "\n".join(
        f"| {r['count']} | {r['metrics']['settled']} | {r['execution_seconds']:.3f} | {r['payments_per_second']:.2f} | {r['native_audit_seconds']:.3f} | {r['standalone_replay_seconds']:.3f} | {r['peak_client_bytes'] / 1048576:.2f} |"
        for r in ladder
    )
)
common = f"\n\nCurrent full regression: {suite['tests']} passed, zero failures/errors/skips, {suite['time']} seconds. One existing Starlette/AnyIO deprecation warning. Source revision: SHA256_MANIFEST.json in review ZIP; frozen baseline source bytes independently match the preserved R3 archive. No frontend rebuild, Docker work, routing-policy tuning or schema migration in R4.\n"
reports = {
    "PERFORMANCE_BASELINE": """The original master prompt describes a suggested seeded10000/~60-second engineering target without fixing one rail mix. The preserved executable stress benchmark is atomic PvP: scenarios/stress_10000.json, seed20261029, principal10000 minor units with jitter1000, source fee20, USD/SGD133/100, normal funded network. Existing P0/P1A-R evidence identifies it as atomic, not three-hop correspondent execution. The current scenario-model defaults and funding are saved in each raw benchmark record.

Historical harness timing includes world initialization/create_run and batched step(...,1000), excludes schema migration, retrospective reconciliation/replay, metrics and evidence serialization. All counted completions must be durably SETTLED. That contract is preserved. Smaller tiers change only payment_count, not quote/routing/funding assumptions. Every measured native intent settled. Atomic measurements are native engine time, not API/browser timing.

A separate frozen OPT07 fixed-order25-intent mixed workload exercises independent atomic/correspondent/instant worlds, including mandatory routing audits. It produces24 completions, one rejection and zero pending, identically before/after. Its rejection is included, not dropped. It is never evidence that a10000-payment mixed workload passed. Freeze: r4_evidence/contract.json; baseline_audit.py and baseline_store.py; original source identity checked against R3 archive. Prechange profile/baselines retained; hash-only intermediate results separately labeled matched_hash_only.json.
""",
    "BOTTLENECK_ANALYSIS": """Measured mixed baseline:25 intents,235 comprehensive audit/replay calls,33.229 cProfile-instrumented seconds. state_digest470 calls consumed18.615 inclusive seconds; replay23.203 and reconcile26.954 inclusive seconds overlap. cProfile substantially amplifies the Python per-token hash loop; those times must not be treated as uninstrumented savings. Direct same-snapshot hash CPU comparisons and unprofiled matched ABBA runs independently verify the benefit. Raw baseline_profile.json and digest_compare.json retained.

Initial atomic1000:16.6355s execution,40390 cursor calls and11.0164s measured cursor time. Journal insertion/projection updates were issued individually per line. Existing events/outbox already use executemany; R4 does not claim to introduce that earlier optimization. A read-only EXPLAIN ANALYZE BUFFERS event-page query used the existing index/bitmap scan plus small in-memory sort:0.254ms execution,34 shared-hit blocks, no shared reads. No measured need for a new index; no database settings changed.

Final component_profile.json profiles one25-payment atomic diagnostic: quote/liquidity/lock reads, journal writes, projection updates, event persistence and other SQL grouped by phase; complete Python call/self/inclusive timings include prepare, settle, journal, event, flush_events, transaction, commit and connection functions. Commit counters cover initialization plus verification too. Standalone audit, replay and JSON evidence serialization are timed separately. Native routing time is zero by workload definition; mixed routing decision timers are retained in run_policy results. SQL time includes driver/roundtrip/server work, not isolated server CPU. Lock-wait duration was not separately isolated; benchmark worlds have no competing application worker. Snapshot/result construction is included in the respective Python profiles; overlapping times must not be added.
""",
    "IMPLEMENTATION": """Two measured, narrowly scoped changes:

1. audit.state_digest emits canonical sorted JSON delimiters/keys and uses the C JSON encoder per bounded financial record, with an extra namespace level for correspondent books. It avoids per-scalar Python generator/hash calls without creating one JSON string for the whole financial snapshot. Chunk memory is bounded to the largest encoded record/subtree, not a strict constant-byte bound. Non-string-key mappings fall back to standard canonical encoding. All integer, Unicode, escaping, list and nested financial test hashes exactly match the original encoder.

2. Transaction.journal batches line INSERTs and matching position UPDATEs within each existing posting group using parameterized executemany. Every original row and update remains, in the original financial transaction under the same run lock. Journal headers, line identities, issuer/currency constraints, deferred balance checks, event payloads and event order remain unchanged. No cross-payment commit relaxation, altered settlement economics or financial fast path.

All authoritative reads, liquidity/reservation validation, quote/finality rules, independent audit, event-chain checks and canonical replay remain in place. No cache or event-head shortcut. Corruption-detection timing unchanged. No audit call removed. The safe performance gains remain much smaller than the original throughput gap.
""",
    "DIFFERENTIAL_CORRECTNESS": """PASS within the tested native scope. test_r4_hashing.py contains11 cases: seven canonical byte-equivalence cases, atomic/instant pending projection-corruption old/new report comparisons, copied event-tamper diagnostic comparison, and exact original/new journal/projection/event-head comparison using frozen Store source. Original and modified full auditor outputs, including findings, replay/state hashes and corruption diagnostics, match exactly. Financial projection corruption is independently rejected; restored states pass. The copied-tamper test alters verifier input, not stored append-only history.

Matched mixed ABBA runs use identical input hashes and canonical normalized metrics/outcomes/route decisions/history hashes. All four produce the same canonical result hash with24 completed/1 rejected/0 pending. Every resulting control and rail world passes independent SQL audit; old/new full reconciliation objects compare exactly. Atomic500 ABBA runs match complete state hashes, event heads, workload hashes, counts and logical SQL operation totals. Five-payment direct state-object comparison also verifies exact account/journal/quote/decision/reservation equality, not just status strings.

Targeted native regression68 passed, including three rails, real database-session contention and genuine process crash recovery. Full regression retains G11/G12 corruption, quote, finality, duplicate and source-final obligation controls plus R3 live API workflows. Existing browser evidence retained; no new browser acceptance claim or frontend rebuild was made.
""",
    "BENCHMARK_RESULTS": f"""Final unprofiled matched ABBA medians:

| Workload | Baseline s | Modified s | Reduction |
|---|---:|---:|---:|
| Atomic500 native execution | {old_a:.6f} | {new_a:.6f} | {100 * (old_a - new_a) / old_a:.2f}% |
| Mixed25 total with mandatory routing audits | {old_m:.6f} | {new_m:.6f} | {100 * (old_m - new_m) / old_m:.2f}% |

Two repetitions per implementation, reversed order. Atomic baseline range7.3955–7.7021s, modified6.9333–7.0739s; mixed baseline11.4080–11.7462s, modified10.1043–10.5011s. Ranges do not overlap in these bounded trials; this is not a universal speedup or statistical confidence interval. Hash-only intermediate had a separate15.9% mixed reduction and is not substituted for final combined results. Cursor calls at atomic500 fall20022→17014 while logical SQL executions remain33082. Cursor calls are not server statement counts: executemany retains every logical row operation.

Final native ladder (500 row displays the latest modified repeat; matched500 median above is the aggregate):

{table}

All intents in these native tiers settled; independent audit and replay passed.1000 has2004 journals and14034 events. Standalone replay is additional measurement work; native audit already includes replay. Raw JSON separates execution, native full audit, standalone replay, independent audit and client CPU. Initial hash-only atomic ladder and raw intermediate runs remain separately named; do not mix them into the final table.

10000-IN-60: UNVERIFIED / NOT ATTEMPTED. Final1000 completes in14.9994s,66.67/s, below required166.67/s. The remaining known SQL/roundtrip cost does not support a plausible60-second10000 attempt. This is a resource/time-gated non-execution, not a measured10000 failure or extrapolated pass. No5000 or10000 tier launched. Native execution still spends9.934s of15.0s in cursor operations at1000; the original ambition remains unmet.
""",
    "RESOURCE_REPORT": """Measured Windows/Python3.12.10/PostgreSQL18.6 host. Migration007. PostgreSQL fsync, full_page_writes and synchronous_commit all ON; durability not weakened. Same existing synthetic database/server/config for before/after, distinct new worlds. Existing database population and warmed caches are limitations; no claim of fresh/empty-server performance. Actual dependency versions and baseline/current source hashes in implementation_manifest.json.

Physical RAM16852574208 bytes. Initial available RAM2845196288 bytes and disk41818292224 bytes. Final1000 before/after available RAM3251007488/3193622528; free disk41577357312/41540460544. Client peak133005312 bytes (126.84MiB) includes full benchmark process verification/high-water, not just execution. PostgreSQL server memory is excluded. Mixed ABBA peaks share a process and are monotonic high-water values; they cannot establish memory savings. Fresh-process atomic500 peaks are approximately100.3–100.6MB for both implementations; no material memory-improvement claim.

Final1000 client CPU9.8125s over14.9994 wall seconds; reported CPU percentage is relative to one core, not total host utilization. Database CPU was not isolated. SQL cursor durations include server/driver/network wait and overlap CPU; do not sum percentages. Process inventory before ladder is saved without command lines/credentials. No duplicate benchmark launched. Benchmarks sequential; each child has90/120s limits, mixed comparison180s, and RAM gate1GiB. STARTED attempt records preserve native run IDs if interrupted; complete reports identify successful tiers. No benchmark timeout occurred.10k escalation rejected on measured throughput/time plausibility, not a fabricated RAM outage. No allocation tracing, GPU use, global PostgreSQL tuning or unrelated-process termination.
""",
    "FINAL_GO_NO_GO": f"""PERFORMANCE OPTIMIZATION: VERIFIED IMPROVEMENT.

FINANCIAL CORRECTNESS: PASS within the tested differential/regression scope.

ORIGINAL10000-IN-60-SECONDS TARGET: UNVERIFIED.

Bounded performance GO: atomic500 median {old_a:.3f}→{new_a:.3f}s ({100 * (old_a - new_a) / old_a:.1f}% lower); mixed25 median {old_m:.3f}→{new_m:.3f}s ({100 * (old_m - new_m) / old_m:.1f}% lower). Financial state/event hashes and detailed old/new audit outputs match; independent audit and all maintained regressions pass. No omitted verification, lost postings, changed fees or optimizer tuning.

This does not establish original-vision completion or large-scale readiness. The remaining native obstacle is thousands of SQL operations/roundtrips per small batch and associated Python/database processing; final1000 remains66.67/s versus166.67/s required. Further changes need their own measured safety proof. No need to repeat the old720s chaos workload or frontend work. Recommended final release-environment work: supported local release smoke and remaining container gate if its daemon becomes available, with current throughput envelope disclosed. External publication remains unauthorized.

Reproduce targeted tests: python -m pytest -q tests/test_r4_hashing.py. Profile freeze only once: python -m experiments.r4_profile (refuses to overwrite baseline). Final matched campaign: python -m experiments.r4_campaign (writes only R4 evidence, child timeouts/resource gates). Small component diagnostic: python -m experiments.r4_components. Full regression: python -m pytest -q --junitxml=docs/THUNDERBOLT_R4_pytest.xml. Quality: ruff check railstorm tests and R4 helper files; ruff format --check same; mypy --explicit-package-bases railstorm tests and six R4 helper files. Synthetic database URL supplied through existing environment variable, never packaged. Review archive checks every required report, CRC and all SHA256 entries; all prior archives preserved.
""",
}
for name, body in reports.items():
    (docs / f"THUNDERBOLT_R4_{name}.md").write_text(
        "# THUNDERBOLT-R4 " + name.replace("_", " ") + "\n\n" + body + common, encoding="utf-8"
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
files += list(docs.glob("THUNDERBOLT_R4_*")) + [
    p for p in evidence.iterdir() if p.is_file() and p.suffix in (".json", ".py")
]
files += [
    docs / "FINAL_prior_archives.json",
    docs / "IMPLEMENTATION_STATUS.md",
    docs / "P1A_R_MATCHED_BENCHMARK.md",
]
contents = {}
for path in sorted(set(files)):
    if not path.is_file():
        continue
    data = path.read_bytes()
    if path.suffix == ".xml":
        data = re.sub(rb'hostname="[^"]*"', b'hostname="REDACTED"', data)
    if any(
        pattern in data
        for pattern in (b"C:" + b"\\Users\\", b"PRIVATE-HOSTNAME-PLACEHOLDER", b"BEGIN " + b"PRIVATE KEY")
    ):
        raise RuntimeError("PRIVATE_DATA: " + path.relative_to(root).as_posix())
    contents[path.relative_to(root).as_posix()] = data
manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
target = root.parent / "RAILSTORM_THUNDERBOLT_R4_REVIEW.zip"
if target.exists():
    raise FileExistsError(target)
with ZipFile(target, "w", ZIP_DEFLATED) as z:
    for name, data in contents.items():
        z.writestr("railstorm/" + name, data)
    z.writestr("railstorm/SHA256_MANIFEST.json", json.dumps(manifest, indent=2))
with ZipFile(target) as z:
    assert z.testzip() is None
    assert all("docs/THUNDERBOLT_R4_" + name + ".md" in manifest for name in reports)
    for name, sha in manifest.items():
        assert hashlib.sha256(z.read("railstorm/" + name)).hexdigest() == sha
print(
    json.dumps(
        {
            "files": len(manifest) + 1,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "prior_archives_verified": len(prior),
            "mixed_reduction_percent": implementation["mixed_reduction_percent"],
            "atomic500_reduction_percent": implementation["atomic500_reduction_percent"],
        }
    )
)
