"""Generate the baseline evidence table directly from accepted measurement files."""

import json
from pathlib import Path

root = Path("experiments/reports/p01")
rows = []
for count in (100, 1000, 5000, 10000):
    r = json.loads((root / f"baseline-verified-{count}.json").read_text())
    db = r["sql_and_transactions"]["execution"]
    rows.append(
        f"| {count:,} | {r['execution_seconds']:.6f} | {r['payments_per_second']:.2f} | {r['events_per_second']:.2f} | {r['row_counts']['events']:,} | {db['sql_count']:,} | {db['commit_count']} / {db['rollback_count']} | {db['mean_transaction_seconds']:.6f} / {db['max_transaction_seconds']:.6f} | {r['client_mean_cpu_one_core_percent']:.2f}% | {r['whole_process_memory_after_audits']['client_peak_rss_bytes'] / 2**20:.2f} | {r['standalone_replay_seconds']:.6f} | {r['reconciliation_including_replay_seconds']:.6f} |"
    )
document = (
    """# P0.1 trustworthy baseline

## Reproduction

The unchanged source was copied before core edits and imported with the harness `--source` option. Original 37 tests passed on new disposable PostgreSQL data. All four baseline volumes settled every requested payment, with zero independently unbalanced journals, zero invalid two-leg groups, zero reconciliation errors and matching recorded replay. The 10K event and financial-state hashes exactly match the retained original P0 result.

```text
python experiments/p01_forensics.py --source <unpacked-p0-source> --counts 100 1000 5000 10000 --label baseline-verified
python experiments/p01_forensics.py --source <unpacked-p0-source> --counts 1000 --label baseline-repeat
```

Use new labels and a disposable database for repeats; existing history is never reset. `experiments/baselines/p0-source.zip` preserves measured modules; every module matches its reported source SHA-256. `experiments/p01_plans.py` retains read-only PostgreSQL plans. Initial instrumentation failures produced no accepted measurement report and are disclosed in FOUNDATION_AUDIT.

## Actual measurements

| Payments | Engine seconds | Payments/s | Events/s | Event rows | Client SQL calls | Commits / guard rollbacks | Tx mean / max seconds | Client CPU / one core | Process peak through audits MiB | Standalone replay s | Reconcile incl. replay s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
"""
    + "\n".join(rows)
    + """

The unchanged 1K repeat took **41.632878s**, versus 41.606620s initially (~0.063% difference). There was one original-source 10K run this sprint, plus the distinct historical report; they are not interchangeable repeated controlled trials. The final hardened implementation is measured separately, including a fresh-process repeat.

## Independent durable counts

At 10K: **10,000 SETTLED payments; 20,004 journal headers (20,000 settlement legs + 4 opening entries); 50,008 journal lines (50,000 economic lines + 8 opening lines); 20,000 reservations; 10,000 decisions; 140,034 event rows; 140,034 outbox rows.** Separate participant/operator claim totals remain USD 1,200,000,000 minor and SGD 1,100,000,000 minor; EUR and INR zero. Opening offsets preserve the closed signed domain.

The healthy fixture has zero database retry attempts and zero SQLSTATE conflicts. RECOVER and EXPIRY events still execute as deterministic no-op/recovery checks; no financial persistence or final audit was bypassed. All report JSON files include explicit row counts, error SQLSTATEs, configuration and source hashes.

## Environment

Native local PostgreSQL **18.6**, Python **3.12.10**, Windows 11, Intel Core **i7-13620H**, **16 logical CPUs**, **16,852,574,208 bytes physical RAM** reported by Windows (~15.70 GiB). SQLAlchemy **2.1.6**, psycopg **3.3.6**, FastAPI **0.115.6**, Pydantic **2.10.5**. PostgreSQL `fsync=on`, `synchronous_commit=on`, `shared_buffers=128MB`. Database is a new workspace-local synthetic database, not a remote/containerized service or user financial database.

CPU percentage in this table is measured Python process CPU time / engine wall time, with 100% meaning one full core; it excludes PostgreSQL server CPU. The final harness additionally records whole-system CPU using Windows GetSystemTimes, explicitly including unrelated workloads. Per-backend PostgreSQL CPU was not isolated. RAM uses Windows PeakWorkingSetSize for the client process, not combined server/shared buffers. Multi-volume runs share a process, so high-water figures include earlier volumes/audits; do not call them per-volume incremental allocations. Final fresh-process 10K measurements clarify scope. Server memory was not separately sampled.

Native database population grows as evidence is retained; OS/PostgreSQL caches and background activity are uncontrolled. Benchmarks are sequential and core workload/persistence/seed is identical, but first final-series measurements overlapped later verification work. The fresh-process final repeat runs after other test jobs finish. No timings from Docker or another machine are mixed into the speedup calculation.

## Historical 470.71 seconds

The preserved historical report is genuine evidence of a 470.709346s execution with a 23.150240s audit. Today's unchanged source took **269.890410s** (37.05 payments/s). That difference occurred before any accepted core optimization, so it is **not credited as a P0.1 speedup**. Earlier work lacked SQL/CPU profiling and had concurrent development activity and different last-minute source hardening. It is impossible to assign an exact causal decomposition to that old number retrospectively. Current profiles identify measured bottlenecks rather than pretending to reconstruct missing historical observations.

## Timing boundary

Engine time includes scenario initialization, demand compilation, all scheduled virtual events, PostgreSQL journals/decisions/holds/events/outbox and durable cursor commits. The original benchmark excluded final reconciliation from its engine number; this sprint retains that boundary for comparison and separately publishes every correctness/audit duration. Standalone replay and reconciliation (which includes another complete replay) are additional real work, not removed tests. Engine + reconciliation is also reported in final acceptance. Virtual p95 remains 50 ms by fixture construction and is never used as simulator throughput.
"""
)
Path("docs/P0_1_PERFORMANCE_BASELINE.md").write_text(document, encoding="utf-8")
print("Generated baseline table from measured reports")
