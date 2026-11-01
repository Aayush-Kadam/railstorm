"""Compute R7 verdicts from preserved actual runs and financial evidence."""

import hashlib
import json
import re
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r7_evidence"


def readable(content):
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", content)
    words = "Matched|matched|native|source|all|Full|full|For|for|The|the|same|within|across|claim|to|from|by|requires|required|before|below|above|at|worlds|workers|roughly|under|passed|verifies|controls|coupled|intents|their|with"
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\b(" + words + r")(?=\d)", r"\1 ", parts[index])
        parts[index] = re.sub(r"(?<=[;:])(?=\d)", " ", parts[index])
        parts[index] = re.sub(
            r"(?<=\d)(?=(?:seconds|payments|tests|errors|GiB|MiB|MB|GB)\b)", " ", parts[index]
        )
    return "".join(parts)


def main():
    context = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    for old in context["prior_archives"]:
        assert hashlib.sha256((ROOT.parent / old["name"]).read_bytes()).hexdigest() == old["sha256"]
    assert (
        hashlib.sha256((ROOT / ".work/r7/prechange_source.zip").read_bytes()).hexdigest()
        == context["checkpoint_sha256"]
    )
    suite = ET.parse(ROOT / "docs/R7_pytest.xml").getroot().find("testsuite")
    assert suite is not None and all(suite.get(k) == "0" for k in ("failures", "errors", "skipped"))
    quality = json.loads((OUT / "quality.json").read_text(encoding="utf-8"))
    assert all(quality[k]["returncode"] == 0 for k in ("lint", "format", "maintained_types"))
    reference = json.loads((OUT / "differential.json").read_text(encoding="utf-8"))
    assert len(reference) == 12 and all(r["pass"] for r in reference)
    restart = json.loads((OUT / "database_restart.json").read_text(encoding="utf-8"))
    assert restart["phase"] == "RESTART_RECOVERY_RETRY_PASS"
    environments = json.loads((OUT / "environment.json").read_text(encoding="utf-8"))
    assert (
        environments["migration_head"] == "008"
        and len(environments["validation_source_matches_release"]) == 95
    )
    summary = {}
    all_rows = []
    primary_files = []
    runtime = {
        p.relative_to(ROOT / "railstorm").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "railstorm").rglob("*.py")
    }
    for count, prefix in ((100, "accepted"), (500, "stable1"), (1000, "stable1")):
        rows = []
        for index, label in enumerate(("baseline", "modified", "modified", "baseline")):
            name = f"{prefix}-{count}-{index}-{label}.json"
            row = json.loads((OUT / name).read_text(encoding="utf-8"))
            primary_files.append(name)
            if label == "modified":
                assert row["source_sha256"] == runtime, "Timed runtime differs from accepted source"
            assert row["counts"] == {
                "payments": count,
                "quotes": count,
                "decisions": count,
                "reservations": count * 2,
                "journal_entries": count * 2 + 4,
                "journal_lines": count * 5 + 8,
                "events": count * 14 + 34,
                "outbox": count * 14 + 34,
            }
            assert row["python"]["event_hash_calls"] == count * 14 + 34
            rows.append(row)
        for field in ("financial_state_hash", "counts", "metrics", "workload_hash", "settings"):
            assert len({json.dumps(row[field], sort_keys=True) for row in rows}) == 1, (count, field)
        assert len({row["audits"]["replay"]["result"]["event_hash"] for row in rows}) == 1
        cohort = {}
        for label in ("baseline", "modified"):
            values = [r for r in rows if r["implementation"] == label]
            times = [r["execution_seconds"] for r in values]
            cohort[label] = {
                "times": times,
                "median_seconds": statistics.median(times),
                "range_seconds": [min(times), max(times)],
                "throughput_at_median": count / statistics.median(times),
                "client_cpu_seconds": [r["cpu_seconds"] for r in values],
                "driver_wait_seconds": [r["driver"]["wait_seconds"] for r in values],
                "driver_wait_calls": [r["driver"]["wait_calls"] for r in values],
                "cursor_calls": [sum(group["calls"] for group in r["sql"].values()) for r in values],
                "financial_commits": [len(r["driver"]["commits"]) for r in values],
                "commit_total_seconds": [sum(r["driver"]["commits"]) for r in values],
                "pool_peak": [r["pool"]["max_checked_out"] for r in values],
                "peak_client_bytes": max(r["peak_client_bytes"] for r in values),
                "native_audit_seconds": [r["audits"]["native"]["seconds"] for r in values],
                "replay_seconds": [r["audits"]["replay"]["seconds"] for r in values],
                "independent_audit_seconds": [r["audits"]["independent"]["seconds"] for r in values],
            }
        cohort["elapsed_reduction_percent"] = 100 * (
            1 - cohort["modified"]["median_seconds"] / cohort["baseline"]["median_seconds"]
        )
        cohort["speed_factor"] = cohort["baseline"]["median_seconds"] / cohort["modified"]["median_seconds"]
        cohort["ranges_nonoverlapping"] = (
            cohort["modified"]["range_seconds"][1] < cohort["baseline"]["range_seconds"][0]
        )
        summary[str(count)] = cohort
        all_rows += rows
    assert summary["1000"]["ranges_nonoverlapping"]
    (OUT / "matched_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    controls = {}
    for label in ("baseline", "modified"):
        value = json.loads((OUT / ("concurrency-" + label + ".json")).read_text(encoding="utf-8"))
        assert len(value["cases"]) == 18 and max(value["forced_shortage_waits"]["observed_lock_waits"]) == 2
        assert all(
            c["errors"] == c["retries"] == 0
            and c["financial"]["independent"]["pass"]
            and c["financial"]["replay"]["match"]
            for c in value["cases"]
        )
        controls[label] = [
            {
                "kind": c["kind"],
                "workers": c["workers"],
                "quote_updater_workers": c["quote_updater_workers"],
                "seconds": c["elapsed_seconds"],
                "completed_unique": c["completed_unique"],
                "attempts": c["attempts"],
                "peak_connections": c["peak_checked_out"],
                "max_run_lock_acquisition_seconds": max(c["lock_acquisition_seconds"]),
                "errors": c["errors"],
                "retries": c["retries"],
            }
            for c in value["cases"]
        ]
    (OUT / "concurrency_summary.json").write_text(json.dumps(controls, indent=2), encoding="utf-8")
    target = json.loads((OUT / "target_decision.json").read_text(encoding="utf-8"))
    assert target["verdict"] == "UNVERIFIED" and not target["full_scale_attempted"]
    largest = summary["1000"]
    reduction = largest["elapsed_reduction_percent"]
    factor = largest["speed_factor"]
    best = max(
        r["payments_per_second"] for r in all_rows if r["count"] == 1000 and r["implementation"] == "modified"
    )
    assert best < 10000 / 60
    table = "|Payments|R6 median seconds (range)|R7 median seconds (range)|Elapsed reduction|R7 rate at median time|\n|---:|---|---|---:|---:|\n"
    for tier, entry in summary.items():
        a, b = entry["baseline"], entry["modified"]
        table += f"|{tier}|{a['median_seconds']:.3f} ({a['range_seconds'][0]:.3f}–{a['range_seconds'][1]:.3f})|{b['median_seconds']:.3f} ({b['range_seconds'][0]:.3f}–{b['range_seconds'][1]:.3f})|{entry['elapsed_reduction_percent']:.2f}%|{b['throughput_at_median']:.2f}/s|\n"
    date = "\n\nClient verification date: 2026-11-01, Asia/Calcutta. Raw operational timestamps retain their recorded offsets. Synthetic native research only; no real-money connection or external publication. LICENSE remains all rights reserved. All 21 prior archives and the source checkpoint retain their original recorded hashes.\n"
    commands = """## Exact reproduction commands

Use a fresh checkout copy. Preserve the supplied `docs/r7_evidence` outside the new output path, and do not copy `.work`/environments. Put the checksum-matching R6 review ZIP beside the checkout. Configure `RAILSTORM_DATABASE_URL` privately for an authorized synthetic PostgreSQL role with disposable database creation rights; never select a user/financial database.

```powershell
python scripts/r7_prepare.py
python scripts/r7_run.py provision
python scripts/r7_run.py accepted 100
python scripts/r7_run.py accepted 500 stable1
python scripts/r7_run.py accepted 1000 stable1
python scripts/r7_run.py tests
python scripts/r7_run.py concurrency
python scripts/r7_restart.py
python scripts/r7_differential.py
python scripts/r7_metadata.py
python scripts/r7_quality.py
python scripts/r7_report.py
python scripts/r7_package.py
```

The first campaign was checkpointed while its runtime still exactly matched R6. The reproduction helper extracts the verified R6 runtime for the baseline even when invoked from modified R7 source. New contexts receive new database names; provision refuses existing databases. Commands and results must not be reused across source versions. Historical source/version evidence and the frozen contract are in `baseline.json`, `contract.json`, `environment.json`; raw command logs are included. The optional Windows protocol trace failed as unsupported and is not required to reproduce the timing results. `r7_restart.py` uses the existing workspace PostgreSQL binaries but initializes/restarts/stops only its new private cluster.

For diagnostics actually executed: `python scripts/r7_run.py bench baseline-diagnostic-100 --implementation baseline --count 100 --profile` and the analogous `candidate-diagnostic-100 --implementation modified` supplied cProfile evidence, excluded from the table. Resource-blocked attempts and a bounded wait are retained separately. Log names are immutable; use a fresh context or a new cohort prefix for a deliberate repeat. The exact primary files are listed in `primary_files.json`.\n"""
    primary = {
        "files": primary_files,
        "method": "ABBA each tier, two samples per implementation; every sample retained",
        "exploratory_not_in_medians": [
            p.name
            for p in OUT.glob("*.json")
            if p.name.startswith(
                ("matched-100-", "baseline-diagnostic", "candidate-diagnostic", "baseline-500-initial")
            )
        ],
    }
    (OUT / "primary_files.json").write_text(json.dumps(primary, indent=2), encoding="utf-8")
    for filename in ("R7_PERFORMANCE_ROOT_CAUSE.md", "R7_ARCHITECTURAL_DESIGN.md"):
        initial = OUT / ("initial_" + filename)
        if not initial.exists():
            initial.write_bytes((ROOT / "docs" / filename).read_bytes())
    root_cause = (OUT / "initial_R7_PERFORMANCE_ROOT_CAUSE.md").read_text(encoding="utf-8")
    root_cause = root_cause.replace(
        "Pending resource-gated repeats and their final outcomes will be recorded in the benchmark report.",
        "Final resource-gated repeat outcomes are recorded below and in the benchmark report.",
    )
    root_cause += f"\n## Final evidence\n\n{table}\nAt 1,000 payments, {reduction:.2f}% lower median elapsed time ({factor:.2f}× rate factor) is verified against the same-session R6 controls; both observed candidate times are lower than both controls. All financial effects and event commitments match. This does not compare R4/R6 historical sessions to R7. Post-recovery resources permitted the final 500/1,000 windows. Larger tiers remain unjustified.\n\n"
    root_cause += "For the native pipeline, per-query cursor timing is submission/driver cost, not isolated server completion time. Connection.wait includes fetch/sync progress and overlaps those costs; do not sum nested metrics. The actual commit calls include deferred commit work. Financial scopes/commits and rail/guard checkouts remain unchanged. The first diagnostic's pool acquisition0.0 was a placeholder, explicitly excluded in diagnostic_annotations.json; all primary cohorts measure pool.connect elapsed. The libpq trace was unsupported on Windows, so no physical RTT or protocol-boundary count is claimed. Post-run read-only EXPLAIN ANALYZE/BUFFERS plans are in read_query_plans.json; aggregate statement planning statistics were unavailable without changing server configuration.\n"
    architecture = (OUT / "initial_R7_ARCHITECTURAL_DESIGN.md").read_text(encoding="utf-8")
    architecture += f"\n## Selection outcome\n\nThe single selected change established a useful {reduction:.2f}% improvement at 1,000 payments. No second independent architectural strategy was implemented. The worker matrix passed safety controls; it is not evidence for substituting a concurrent/sharded workload. New event streams, larger transaction batches and removal of defensive financial checks were rejected.\n"
    reports = {
        "PERFORMANCE_ROOT_CAUSE": root_cause,
        "ARCHITECTURAL_DESIGN": architecture,
        "IMPLEMENTATION_AND_DIFF": """# R7 implementation and diff

The only runtime changes are `railstorm/store.py`, `railstorm/market_quotes.py`, and new `railstorm/native_sql.py`. Native financial calculations, routing decisions, audit/oracle/replay code, schemas, migrations, frontend and lockfiles are unchanged. `runtime.diff` records the exact reversible textual changes; `environment.json` verifies accepted runtime/test/scenario/migration bytes match the fully tested copy.

The native adapter uses the pinned psycopg3 driver on a pooled SQLAlchemy connection. A bounded512-entry SQL template cache uses SQLAlchemy's dialect compiler (including literal percent, repeated binds and casts); it never caches balances, account projections, quotes, event heads or audit results. Prepared commands and executemany retain all original parameters/row identities. The pipeline exists only inside the already locked transaction and must synchronize/exit successfully before SQLAlchemy can commit. Transaction count and queue batch size are unchanged.

Native result mappings/scalar/rowcount cover the application's existing internal queries. The full SQLAlchemy connection remains available and is synchronized before its result metadata is read. Native error translation keeps SQLAlchemy exception categories and marks rollback-only; fatal owned-backend disconnects invalidate the pool connection. Both caught and deferred errors cannot produce a successful financial commit. No retry/conflict suppression is added.

The first focused candidate exposed an outbox fault-hook regression (42 passed,1 failed). That candidate was not accepted. Existing before/after cursor fault/monitoring hooks were restored; unknown after-hooks synchronize cursor metadata. Only explicitly marked timing observers can avoid that synchronization. The corrected focused suite passed44, followed by the full268-test regression. The standalone protocol diagnostic's unsupported trace attempt is retained as unavailable, not a successful financial/performance sample.

The template/helper interfaces target this pinned synthetic application, not an assertion of complete third-party SQLAlchemy Result/ExecutionContext API equivalence. External monitoring hooks can force synchronization and reduce the measured speedup. No dependency, infrastructure configuration or public HTTP API was changed. Restoring the prechange source checkpoint or using the unchanged R6 review archive reverses the runtime change.
""",
        "CORRECTNESS_VERIFICATION": f"""# R7 independent correctness verification

FINANCIAL CORRECTNESS: PASS. Full copied-source regression: **{suite.get("tests")} passed**, zero failures/errors/skips, {float(suite.get("time", "0")):.2f} seconds. The existing Starlette/AnyIO deprecation warning remains. The accepted source's95 runtime/test/scenario/migration files match the validation copy. No evidence-generating historical test ran against original report paths; copied validation artifacts are retained separately.

Every primary benchmark world passed native reconciliation, independent SQL financial auditing and deterministic replay. Within each tier, full financial-state hashes, canonical event commitments, complete payment metrics, financial table counts, workload and database settings match between implementations. The independent post-run reference re-read all12 worlds, recomputed seeded amounts and integer floor FX without the financial engine, and checked every source/target posting line, all28 account balances, exact fees, quotes, decisions, consumed reservations, settled+ACK status, virtual settlement timestamps and matching event/outbox coverage.

For1,000 intents:2,004 journal groups including opening,5,008 lines,2,000 consumed reservations,1,000 immutable quotes/COMMIT decisions,14,034 events and matching outbox rows; no rejected/pending/unacknowledged payments. Fees are20,000 USD minor units. Exact source debits, beneficiary proceeds and floor-rounded FX totals are in `differential.json`. Each payment's five economic lines match the independent integer reference. No aggregate-only waiver is used.

The36 concurrency configurations (18 per implementation) passed:1/2/4 workers, disjoint funded currency accounts, shared sources, shared destinations, scarce liquidity, duplicate identities and quote updates. Every case checks exact independently computed positions, native/independent audit and replay. Scarcity completes one intent and rejects11; duplicate attempts produce one payment/two journal groups. Two forced workers were actually observed waiting on the run lock. Quote outcomes preserve the accepted version, even when update/prepare order varies legally. These are diagnostic contracts, not the historical timing queue.

Actual private PostgreSQL stop/start preserved the complete atomic prepared quote, instant source-final/destination-pending13,300-minor-unit obligation and correspondent pending-hop state. The persisted snapshots/event hashes matched exactly before recovery. All three settled after restart; two retries preserved journals/positions. The private server was stopped, its data retained, and the shared server was not restarted for this test. Full regression also includes real worker death, liquidity faults, duplicate delivery, stale quotes, one-minor-unit corruption, event tampering, journal sealing and projection integrity. An unchanged event head never bypasses fresh account checks.
""",
        "BENCHMARK_RESULTS": "# R7 controlled benchmark results\n\n"
        + table
        + f"\nThe largest matched result is **{reduction:.2f}% lower median elapsed time**, a **{factor:.2f}×** rate factor. Each tier uses two controls and two candidates in A/B/B/A order. Ranges are the actual min/max of those two observations, not confidence intervals. No unmatched historical R4/R6 timing is used as proof. Best actual native1,000: {best:.3f}/s; no larger tier was executed.\n\nAll source and event hashes match the financial contract; no failed/blocked sample is silently promoted into the table. The isolated R6 500 diagnostic (18.126s), first exploratory100 crossover, cProfile runs, resource stops and the bounded headroom wait remain separate. These demonstrate host variability and are disclosed rather than used to cherry-pick controls. Background load is not fully quiesced on a personal laptop; whole-host CPU tick samples, client CPU and server CPU/memory samples accompany every primary run. Minor local authoring/formatting overlapped the first500 control; all timings are retained and the later1,000 crossover ran sequentially without another heavy job.\n\nTiming begins before create_run and ends after the final committed step1000. It includes initialization, queue generation/persistence, every native scheduled command, financial checks, journals/projections, event hashing, outbox, and durable commits. Scenario selection, environment/readiness/stat probes and the preflight connection are outside this native window as in P0.1/R6; additional command/guard connection acquisition inside execution is timed. HTTP/browser overhead is not part of the historical native harness. Retrospective audit/replay durations are separately recorded, never substituted for execution. No aggregate cross-run total is an acceptance result: each row is its own qualifying workload.\n\n`matched_summary.json` records each timing, CPU, driver wait/submit counts, actual commit latencies, pool peak and audit/replay durations. Individual raw JSON files contain SQL groups, driver calls, event hash/serialization time, lock spans and database/WAL observations. Native pipeline submission timings and overlapping wait timings cannot be summed as isolated server latency.\n\n"
        + commands,
        "RESOURCE_PROFILE": """# R7 resource profile

The host reports16,852,574,208 bytes RAM. Initial headroom moved from1.67GB to1.10GB, then below1GB; a later read showed roughly1.83GB. Major-run gates stopped those attempts before financial work. The bounded four-minute wait also stopped without launching a benchmark. After recovery, the final500/1,000 primary windows passed the fixed2GiB start gate. Raw resource stops and readings remain separate from successful samples. Gates were not relaxed.

The100 tier requires768MiB; major tiers require2GiB plus3GiB disk. Benchmarks check512MiB at committed batch boundaries. Regression required1GiB at start and had a512MiB watchdog/420-second bound; it passed. Concurrency diagnostics use768MiB for1/2 workers and1536MiB for4; the private server restart required1GiB. All these bounded checks passed where work actually ran. No unrelated application/service was terminated. The initially stopped shared workspace PostgreSQL was restarted without resetting its data.

`preflight-*.json` captures host RAM/CPU snapshots, PostgreSQL process working sets/CPU, settings/waits, Docker status, extension availability and disk space. Whole-host CPU was about2.6–3.7% during the short initial preflight samples; per-run tick counters show later load. Primary worker count is one; pool peak is two for the financial transaction plus queue guard. Concurrency max is four financial workers plus one quote updater, with measured pool peaks. Eight workers were not justified behind the same run mutex. Server process memory sums include shared pages; server CPU/WAL are not exclusively attributed to a world. Client RSS high-water is separately labeled. The raw peak_client_bytes is worker lifetime high-water measured after audits/snapshot, an upper bound for execution memory rather than an isolated payment-only peak. Driver rollback-call totals include idle pool resets and are not physical transaction counts; actual financial commits and rail/guard connection scopes are separately identified.

Docker engine was unavailable; Docker/WSL headroom and R7 container execution remain UNVERIFIED. No Docker infrastructure was started to force a profiling result. No pg_stat_statements extension/preload or I/O timing change was enabled, and no global profiling restart was performed. Read-only EXPLAIN ANALYZE/BUFFERS plans were collected post-run. Full libpq trace is unsupported by the Windows binary; this is an explicit unavailable metric, not a zero count.

All synthetic data/worlds and the private stopped restart cluster are retained. Prior21 review archives and immutable R6 source/reproduction materials remain intact. The native server is left available; no destructive cleanup touched prior databases or protected data.
""",
        "10K_GO_NO_GO": f"""# R7 10,000-payment go/no-go

**NO-GO for a full-scale attempt. ORIGINAL 10,000-IN-60: UNVERIFIED.**

The frozen atomic USD/SGD contract requires10,000 distinct, genuinely settled and acknowledged native intents, all required durable postings/quotes/decisions/reservations/events/outbox, completion inside60 seconds, and passing independent audit/replay. The expected full-scale counts are in the pre-implementation contract; none were replaced with counters or mock completion.

The best measured native1,000 rate was **{best:.3f}/s**, versus166.667/s required. Candidate500 achieved76.87–85.71/s; candidate1,000 achieved55.48–60.32/s. Measured scaling provides neither the target rate nor useful headroom. Client CPU for1,000 was11.922–12.609 seconds, already above the6-second-per1,000 budget equivalent to the target before considering remaining waits. These are measured costs, not a fabricated10,000 timing.

The2,500/5,000/10,000 tiers were not launched. Machine gates require at least two prior candidate observations with180/s headroom and passing financial verification before any larger tier. No projected passing or failing full-scale result is recorded. `target_decision.json` contains the exact decision and observed rates.

The single selected architectural change was useful and is retained. Its benefit is insufficient to achieve the original ambition on the measured host. More workers in the same world still contend on the mandatory run/event-order mutex; independent-world sharding changes this benchmark. A plausible further investigation would need to reduce residual per-payment Python copying/serialization and native statement work, or demonstrate an equivalent set-based executor with every financial check and original visibility/recovery behavior. A new sequencing model or dropped writes is not an acceptable shortcut. This campaign does not implement a second speculative financial engine, and no future implementation is guaranteed to succeed.
""",
        "FINAL_VERDICT": f"""# R7 final verdict

|Acceptance|Verdict|Evidence|
|---|---|---|
|Architectural optimization|VERIFIED IMPROVEMENT|Matched1,000 median {largest["baseline"]["median_seconds"]:.3f} to {largest["modified"]["median_seconds"]:.3f}s; {reduction:.2f}% lower; ranges separated|
|Financial correctness|PASS|268 regression tests;12 independent integer-reference worlds; exact event/journal/state equivalence|
|Concurrency safety|PASS|36 diagnostic configurations across1/2/4 workers; scarcity/duplicates/quote races; actual observed run-lock waits|
|10,000 payments within60 seconds|UNVERIFIED|No valid full-scale attempt; best measured native1,000 {best:.2f}/s does not justify larger tiers|
|Existing synthetic research preview|PRESERVED|Native/API/Live Crisis/all-rail regression and actual private database restart/recovery; R5/R6 archives unchanged|
|Original expanded flagship vision|PARTIAL|Useful architectural progress does not establish original throughput or broader full-vision completion|

The actual runtime change is a prepared native command/pipeline adapter under the existing SQLAlchemy-owned financial transaction. No financial formula, check, audit, journal/event duty, batch boundary, durability setting, dependency or frontend was weakened. The first hook regression was found and fixed before acceptance; all failed/stopped attempts remain disclosed.

Maintained lint/format/types pass. The broader type inventory still contains the same41 errors in ten legacy scripts; no universal type-clean claim is made. R7 Docker/WSL execution and a new browser campaign were not verified; preserved R5/R6 container/browser evidence remains historical, not relabeled as an R7 run. The preview verdict is scoped to the exercised native/API/financial/recovery workflows and preservation of those prior releases. No publication or license change occurred.

Stop this campaign after one useful measured architectural change. Retain the verified adapter and the reversible R6 checkpoint; do not claim10,000 success or launch predictable oversized runs. Further performance work would require a new independently justified correctness-preserving design, not repeated small tuning without a path to166.67/s.
""",
    }
    # The historical baseline/preview text is evidence, not a new run. Keep edits
    # confined to R7 working reports and selected new metadata.
    for name, content in reports.items():
        (ROOT / ("docs/R7_" + name + ".md")).write_text(readable(content + date), encoding="utf-8")
    verdict = {
        "architecture": "VERIFIED IMPROVEMENT",
        "financial": "PASS",
        "concurrency": "PASS",
        "target10000": "UNVERIFIED",
        "preview": "PRESERVED",
        "expanded_vision": "PARTIAL",
        "largest_elapsed_reduction_percent": reduction,
        "largest_speed_factor": factor,
        "best_native1000_per_second": best,
        "regression_tests": int(suite.get("tests", "0")),
    }
    (OUT / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
