"""Derive R6 acceptance documents from executed measurements, never projections."""

import hashlib
import json
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

from experiments.r4_profile import resources

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/r6_evidence"


def main():
    baseline = json.loads((OUT / "baseline.json").read_text())
    for row in baseline["prior_archives"]:
        assert hashlib.sha256((ROOT.parent / row["name"]).read_bytes()).hexdigest() == row["sha256"]
    quality = json.loads((OUT / "quality.json").read_text())
    assert all(quality[k]["returncode"] == 0 for k in ("lint", "format", "maintained_types"))
    suite = ET.parse(ROOT / "docs/THUNDERBOLT_R6_pytest.xml").getroot().find("testsuite")
    assert suite is not None
    assert all(int(suite.attrib[k]) == 0 for k in ("errors", "failures", "skipped"))
    container = json.loads((OUT / "container_smoke.json").read_text())
    assert container["phase"] == "RESTART_PASS" and container["migration_head"] == "008"
    assert container["database_restart_verified"] and container["application_restart_verified"]
    for implementation in ("baseline", "modified"):
        control = json.loads((OUT / ("contention-" + implementation + ".json")).read_text())
        assert max(control["shortage"]["observed_lock_waits"]) == 2
        assert control["duplicate"]["journal_groups"] == 2
    rows = [json.loads(p.read_text()) for p in OUT.glob("matched-*.json")]
    rows = [r for r in rows if "execution_seconds" in r]
    final = [json.loads(p.read_text()) for p in OUT.glob("final-1000-*.json")]
    final = [r for r in final if "execution_seconds" in r]
    if final:
        assert len(final) == 4, "Final1000 comparison must finish in full"
        (OUT / "interrupted_1000_observations.json").write_text(
            json.dumps([r for r in rows if r["count"] == 1000], indent=2)
        )
        rows = [r for r in rows if r["count"] != 1000] + final
    summaries = {}
    differential = {}
    for count in (25, 100, 500, 1000):
        group = [r for r in rows if r["count"] == count]
        assert group and {r["implementation"] for r in group} == {"baseline", "modified"}
        for key in ("workload_hash", "state_hash", "metrics", "counts", "database"):
            assert len({json.dumps(r[key], sort_keys=True) for r in group}) == 1, (count, key)
        assert len({r["audits"]["replay"]["result"]["event_hash"] for r in group}) == 1
        for r in group:
            assert r["audits"]["reconciliation"]["result"]["financial_errors"] == 0
            assert r["audits"]["independent"]["result"]["pass"]
            assert r["audits"]["replay"]["result"]["match"]
        differential[str(count)] = {
            "matched_runs": len(group),
            "state_hash": group[0]["state_hash"],
            "event_hash": group[0]["audits"]["replay"]["result"]["event_hash"],
            "counts": group[0]["counts"],
            "metrics": group[0]["metrics"],
            "pass": True,
        }
        labels = {}
        for label in ("baseline", "modified"):
            subset = [r for r in group if r["implementation"] == label]
            values = [r["execution_seconds"] for r in subset]
            labels[label] = {
                "n": len(subset),
                "median_seconds": statistics.median(values),
                "range_seconds": [min(values), max(values)],
                "median_payments_per_second": statistics.median(r["payments_per_second"] for r in subset),
                "throughput_at_median_seconds": count / statistics.median(values),
                "cursor_calls": [r["cursor_calls"] for r in subset],
                "median_cursor_seconds": statistics.median(r["cursor_seconds"] for r in subset),
                "median_client_cpu_seconds": statistics.median(r["cpu_seconds"] for r in subset),
                "peak_client_bytes": max(r["peak_client_bytes"] for r in subset),
                "median_reconciliation_seconds": statistics.median(
                    r["audits"]["reconciliation"]["seconds"] for r in subset
                ),
                "median_replay_seconds": statistics.median(r["audits"]["replay"]["seconds"] for r in subset),
                "median_independent_seconds": statistics.median(
                    r["audits"]["independent"]["seconds"] for r in subset
                ),
            }
        reduction = 100 * (1 - labels["modified"]["median_seconds"] / labels["baseline"]["median_seconds"])
        separated = labels["modified"]["range_seconds"][1] < labels["baseline"]["range_seconds"][0]
        summaries[str(count)] = {
            **labels,
            "elapsed_reduction_percent": reduction,
            "nonoverlapping_observed_ranges": separated,
            "complete_abba": len(group) == 4,
        }
    improvement = summaries["500"]["complete_abba"] and summaries["500"]["nonoverlapping_observed_ranges"]
    assert improvement, "Do not assert verified improvement without the matched evidence"
    (OUT / "matched_summary.json").write_text(json.dumps(summaries, indent=2))
    (OUT / "differential.json").write_text(json.dumps(differential, indent=2))
    measured_rate = summaries["1000"]["modified"]["throughput_at_median_seconds"]
    best_rate = max(
        r["payments_per_second"] for r in rows if r["count"] == 1000 and r["implementation"] == "modified"
    )
    decision = {
        "verdict": "UNVERIFIED",
        "attempted": False,
        "required_payments_per_second": 10000 / 60,
        "measured_modified_1000_payments_per_second": measured_rate,
        "best_observed_modified_1000_payments_per_second": best_rate,
        "resources": resources(),
        "reason": "Measured native 1,000-payment throughput is far below166.67/s; smaller-tier scaling does not justify a60s10000 attempt. Host RAM also fluctuated below the resource gate. No10000 result is inferred from extrapolation.",
        "minimum_future_work": "Reduce synchronous per-payment writes and Python orchestration through a native set-based command executor or measured driver integration; preserve every funded check, journal, quote, decision, event, outbox and existing recovery boundaries. This is engineering work with no success guarantee.",
    }
    assert measured_rate < 10000 / 60
    (OUT / "target_decision.json").write_text(json.dumps(decision, indent=2))
    frozen = ROOT / ".work/r6/baseline/railstorm"
    unchanged: dict[str, str] = {}
    changed: dict[str, str] = {}
    for p in sorted((ROOT / "railstorm").rglob("*.py")):
        relative_source = p.relative_to(ROOT / "railstorm")
        same = p.read_bytes() == (frozen / relative_source).read_bytes()
        (unchanged if same else changed)[relative_source.as_posix()] = hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
    assert set(changed) == {"store.py", "market_quotes.py"}
    (OUT / "source_changes.json").write_text(
        json.dumps({"changed": changed, "unchanged": unchanged}, indent=2)
    )
    table = "|Payments|R5 median s (range)|R6 median s (range)|Elapsed reduction|A/B repeats|\n|---:|---|---|---:|---|\n"
    for tier, entry in summaries.items():
        a, b = entry["baseline"], entry["modified"]
        table += (
            f"|{tier}|{a['median_seconds']:.3f} ({a['range_seconds'][0]:.3f}–{a['range_seconds'][1]:.3f})|"
            f"{b['median_seconds']:.3f} ({b['range_seconds'][0]:.3f}–{b['range_seconds'][1]:.3f})|"
            f"{entry['elapsed_reduction_percent']:.1f}%|{a['n']}/{b['n']}|\n"
        )
    scope = (
        "\n\nR5 archives remain unchanged. This campaign is synthetic native Windows/PostgreSQL execution; "
        "it is not a mixed-rail/container throughput claim. Historical R4 native 1,000-payment in 14.999 s was measured in a different session; "
        "the valid R6 speed comparison uses the contemporaneous R5 source controls above. Full-suite/API/Live Crisis checks "
        "cover all supported rails; no frontend source or financial-audit code changed. Public publication was not performed. "
        "LICENSE remains all rights reserved. Verification date2026-10-31 Asia/Calcutta; raw timestamps retain recorded offsets.\n"
    )
    commands = """
## Reproduction commands

Configure `RAILSTORM_DATABASE_URL` privately for the workspace synthetic PostgreSQL server and an authorized database-creation role. Run from the checkout root using its installed Python environment. Do not target a user/financial database.

```powershell
python scripts/r6_run.py provision
python scripts/r6_run.py campaign
python scripts/r6_run.py final1000
python scripts/r6_run.py contention
python scripts/r6_run.py tests
python scripts/r6_quality.py
python scripts/r6_report.py
python scripts/r6_package.py
```

The supplied evidence is immutable input to review, not a fresh output directory. For a new campaign copy the source into a new folder, preserve/move the supplied `docs/r6_evidence` aside, and run `python scripts/r6_prepare.py` first. Place the checksum-matching R5 review ZIP beside the checkout as documented in that script. It extracts the frozen R5 implementation, records source hashes and picks a new database name. Provisioning refuses an existing database; campaign resumes completed measurements, so use a new campaign context to repeat from scratch. The actual first campaign context and pre-implementation source hashes are in `baseline.json`, and the frozen timing/assignment contract is `contract.json`.

`r6_run.py` uses bounded child timeouts, sequential workloads and only the generated R6 database for index controls. `r6_benchmark.py` records each run's source bytes, SQL call/cost breakdown, transaction/pool/lock observations, process CPU/RSS, database settings, financial metrics and independent audit/replay durations. Warmup/cProfile diagnostics and resource-stopped attempts are retained separately and excluded from matched timing medians. No measured result is overwritten.

The actual initial1000 comparison stopped at a RAM gate after two completed samples. `final1000` was preregistered before timing and executes a new complete A/B/B/A window; the interrupted pair remains in separate evidence. On a fresh stable campaign the normal ladder can supply the1000 result without this extra window. Do not blend those cohorts. The per-run rate medians and the rate associated with median elapsed time are separate summary fields; the target decision uses the latter and reports the best actual run independently.

Container reproduction uses a new `context.json` project name beginning `railstorm-r6-`, a free loopback `container_port`, and the generated benchmark database name. Run `python scripts/r6_docker.py` with phases `build`, `up`, `initial`, `host`, `restart`, `collect`, `down`, sequentially. Always execute `down` in cleanup; never use `down -v` on unrelated data. These bounded phases were actually executed, and their full commands/exit codes are in `docker_commands.json`.
"""
    p500 = summaries["500"]
    p1000 = summaries["1000"]
    larger_tier = (
        f"The complete final 1,000-payment comparison has {p1000['elapsed_reduction_percent']:.2f}% lower median elapsed time, "
        f"but observed ranges {'do not overlap' if p1000['nonoverlapping_observed_ranges'] else 'overlap'}. "
        "Because the1000 timing variation is large and the ranges overlap, its timing difference is INCONCLUSIVE; "
        "no verified1000 speedup is claimed. The resource-interrupted initial1000 pair is retained separately in "
        "interrupted_1000_observations.json, not blended with the preregistered final four-run comparison. "
        f"The best actual R6 native 1,000-payment rate was {best_rate:.3f}/s.\n\n"
    )
    reports = {
        "ARCHITECTURAL_BOTTLENECK": """The remaining bottleneck is the serialized native command path's many database cursor operations, alongside Python serialization/copy/orchestration. R5 uses the run row as the financial mutex and commits the queue and economics together in batches of at most1000 virtual events. Multiple workers on one run cannot safely multiply throughput.

The bounded25-payment cProfile diagnostic (`baseline-profile.json`) separates SQL cursor timing and Python function costs. Its leading self cost is socket/select waiting; recursive defensive copies and JSON encoding remain visible. cProfile is excluded from matched results. The 500-payment R5 diagnostic (`a500.json`) took12.447s, including8.309s inside cursor calls; quote-history lookup alone consumed0.748s in500 calls. Repeated market lookups used the general event path, unnecessarily flushing event/outbox batches and scanning unrelated history. Reservation prepare/resolve issued separate projection and reservation writes. Journal insertion and position projection costs remain recorded separately in each raw `sql` map.

Application cursor calls are an exact measured communication-operation count; they are not a packet capture or exact network RTT count. Psycopg executemany can pipeline multiple logical statements inside one cursor call. No wire-RTT number is fabricated. SQL time is client-observed execution/fetch setup, not pure server CPU; deferred commit work remains inside total execution time. Subtracting cursor time from total is not a pure Python CPU measure. Client CPU and a separate all-PostgreSQL-process sample are provided instead.

For all twelve requested categories: cursor maps identify read/write and journal/projection/quote/event costs; transaction counters record commit/rollback/begin counts; run-lock durations record acquired-lock-to-commit-callback spans (excluding final commit latency); cProfile identifies serialization and orchestration; pool checkouts/max simultaneous connections expose reuse; the contention report demonstrates two independent sessions actually waiting on the financial run lock. No heavy tracing is included in timing comparisons.

At 1,000 payments the cursor count fell from 34,382 to 28,382: 6,000 fewer application cursor calls (17.45%). Both still commit six financial transactions including initialization, with 14 total SQLAlchemy transaction scopes and a maximum of two simultaneous checked-out connections. Combining two writes into one CTE changes communication counts, not required financial effects. Journals/lines/accounts/reservations/events remain identical. The final quote access-plan diagnostic on one completed world observed 0.037ms with the partial index versus 9.075ms through the original access path. This single post-execution query diagnostic explains an access-path difference; it is not a payment-throughput claim. Raw plans, row/filter counts and buffer observations are retained in `quote_access_plans.json`.
""",
        "IMPLEMENTATION": """Accepted changes are limited to `railstorm/store.py`, `railstorm/market_quotes.py` and the additive008 index migration.

1. Combine each reservation projection update and reservation insert/transition through a data-modifying CTE. The CTE's count dependency guarantees the projection operation runs; it still produces one final insert even if the old projection predicate matches no rows, preserving the old statement behavior. Every original predicate, database trigger, balance constraint, reservation state transition and financial event remains. Sequential reservation order and the run mutex are unchanged. No amounts are aggregated across accounts or payments.
2. A market query first inspects frozen buffered MARKET_QUOTE payloads in reverse sequence order, then queries committed events with the original pair/type/order predicate. This is the exact read-your-writes merge; it never flushes unrelated events merely to ask for a market quote. Generic event/audit readers retain their old flushing behavior. No quote or account/audit result is cached across transactions, and locked payment-quote verification at settlement remains unchanged.
3. Migration008 adds a partial index on run/pair/descending sequence for MARKET_QUOTE events. It changes an access path, not quote selection, journal semantics or durability. Apply `alembic upgrade head`; old data is preserved.

The exploratory transaction-wide driver pipeline was rejected immediately after ResourceClosedError on the25-payment setup; SQLAlchemy cursor metadata was unavailable in that mode. No pipeline code remains in the runtime. Its failed logs/attempt IDs are retained separately, never counted as completions.

The active native smoke worker now checks the migration head from the source's Alembic directory rather than hardcoding007. This maintains the documented smoke command after adding008. No Docker configuration, dependencies, frontend source, transaction batch size, funding, audit implementation or event canonicalization changed. The R5 archive remains the recovery/reference release.
""",
        "DIFFERENTIAL_CORRECTNESS": f"""FINANCIAL CORRECTNESS: PASS within the exercised synthetic contract.

The copied-source regression suite passed {suite.attrib["tests"]} tests with zero failures/errors/skips, in {suite.attrib["time"]} seconds. It ran from `.work/r6/validation` against the isolated R6 database, so tests which emit old milestone evidence write only into the copied validation checkout. Generated negative/crash evidence is retained under `validation_artifacts`. The initial15 targeted controls also passed; their generated R2.1 files were retained separately and historical files restored from the preserved R2.1 review archive.

Every completed matched run passed full native reconciliation, standalone deterministic replay and the independent SQL financial oracle. For each tier, full financial-state hashes, byte-canonical event commitments, workload hashes, complete metrics and financial table counts match between R5 and R6. Therefore exact balances, beneficiary proceeds, fee currency/amount, prepared/pending/rejected outcomes, journals/lines, decisions, reservations, quote references and event history agree; no representational difference is being waived. `differential.json` records the hashes/counts/metrics and `matched-*.json` retains all underlying audit results.

Controlled negatives include concurrent funded shortage, concurrent duplicate execution, stale quote/version changes, interrupted transactions and real subprocess death/recovery, one-minor-unit projection corruption, and event tampering. The retained tests continue detecting corruption independently of an unchanged event head. New tests prove buffered-versus-persisted quote visibility, two currency pairs, no unrelated event flush, generic event reads, rollback of both CTE writes after duplicate reservation insertion, and independent detection of a one-unit imbalance. A rolled-back batch does not advance its cursor or emit durable economic effects.

`contention-baseline.json` and `contention-modified.json` retain actual blocked backend observations and duplicate-credit checks for both implementations. Journal immutability/sealing, finality protection, API compatibility, all supported rails, FX, Live Crisis and projection/event integrity are exercised by the full regression. No audit gate was replaced or weakened.

The actual Linux R6 container smoke passed on migration 008, using the active native financial worker. Atomic, correspondent, instant, multicurrency, expiry, shortage, lost-ACK retry, pending obligation persistence, locked quote version, financial reconciliation and replay passed. The database/application containers were recreated without deleting their isolated new volumes; recovery then credited the beneficiary once. Host-loopback ingress also passed. `container_source_sha256.json` verifies 40 runtime/migration Python files against the release source. This is an R6 smoke result, separate from preserved R5 browser acceptance; it is not a container throughput benchmark.
""",
        "MATCHED_BENCHMARKS": table
        + larger_tier
        + f"""
The 500-payment median reduction is {p500["elapsed_reduction_percent"]:.2f}%; its two observed R6 times are below both R5 controls. This is a bounded verified improvement at that tier, not a confidence interval or a universal throughput multiplier. Small-tier overlapping ranges are not promoted as verified speedups. The 1,000-payment tier is reported exactly as executed, including repeat count and any resource interruption; it does not inherit the500-tier percentage.

Initial source comparison uses the checksum-verified R5 ZIP, never the pre-R4 implementation. Same amounts, seeded assignments, institutions, quotes, fees, funding, routes, pool defaults, PostgreSQL settings and step1000 schedule are used. New index creation/removal occurs outside the timer on the isolated R6 database to represent each implementation's deployed schema. Account/event/journal/decision/outbox writes and final database constraints remain timed. A/B/B/A is sequential, not concurrent. Host memory fluctuates and the interrupted campaign resumes only unfinished runs; this limits generalization and is disclosed rather than hidden by cherry-picked historical timings.

`matched_summary.json` includes per-tier median/range, CPU/RSS, cursor counts/time, reconciliation/replay/independent durations. Raw rows include every transaction count, lock span, connection-pool observation and SQL text group. PostgreSQL process CPU/RSS includes the whole shared synthetic server and is not an exclusive per-world server measurement. All matched rows report the qualifying completed count and financial correctness.
"""
        + commands,
        "10000_TARGET": f"""ORIGINAL 10,000-IN-60-SECONDS TARGET: UNVERIFIED.

The primary contract remains the historical atomic seeded stress scenario. It includes create_run, queue persistence, every native financial command, durable postings/events/outbox, cursor progress and final commit. Post-execution reconciliation/replay are timed separately as in the original implementation. Mixed-rail routing runs are not substituted or combined.

The measured R6 native 1,000-payment median rate is {measured_rate:.3f} completed payments/second versus the required 166.667. The smaller-tier results do not support a60-second10000 attempt. A10,000 run was therefore not launched; no actual failed timing or passing result is invented. The original target is neither redefined nor markedPASS from an extrapolation. The raw decision and resource state are in `target_decision.json`.

Even with the improved reservation/quote communication path, many serial SQL commands and defensive serialization/copy operations remain for each payment. Actual1000-tier SQL and client CPU times are in the raw reports. A plausible next architectural investigation is a native set-based financial executor or a specifically validated lower-roundtrip driver path, preserving funded reads, immutable quote checks, every journal/decision/event/outbox row and existing recovery boundaries. It would require new equivalence and contention/crash proof. Neither extra threads behind the same run mutex nor a future refactor is promised to reach the target.
""",
        "RESOURCE_REPORT": """Windows host reports 16,852,574,208 bytes physical RAM; free physical RAM fluctuated sharply. Initial readings were near 0.99GB and later dropped below 512MiB. An initial blanket 1GiB exploratory gate stopped25 payments before starting. Before accepted baseline measurements, fixed tier-specific thresholds were adopted for these bounded workloads:512MiB free for25/100,768MiB for500/1000,1536MiB for10000, plus2GiB free disk. Those campaign thresholds were not relaxed after later stops. Completed25/500/1000 client high-water measurements remained roughly68–81MB; whole-server memory is separately scoped.

All workloads are sequential with bounded subprocess timeouts. Instrumented pool maximum is recorded per run (financial transaction plus queue advisory guard); no unbounded concurrent executor was introduced. The explicit contention control uses two workers. Existing PostgreSQL was found stopped and restarted using its existing workspace cluster; prior databases were not reset. A newly named R6 database isolates measurements and index controls. Recovery tests use their own disposable synthetic databases. No destructive cleanup touched unrelated data.

`resources_before/after`, process peak RSS, client CPU, SQL cursor time, transaction/lock spans and shared PostgreSQL process snapshots are recorded in raw results. Server working-set sums can double-count shared pages; CPU samples are server-wide. They are useful resource observations, not exclusive per-payment CPU/peak-memory attribution. Python client high-water memory is not whole-system peak RAM. Raw stopped/failed candidates are separate from accepted timing rows. No incomplete or rejected sample contributes a completion or a speedup.

The copied-source regression started above512MiB free and had a continuous256MiB stop threshold and300-second deadline. It completed264 tests in278.15s without tripping those stops; raw resource samples are retained. Later native benchmark code also checks the256MiB stop at committed batch boundaries. The initial recorded native samples had pre/post checks; no continuous trace is retroactively claimed for them. Container phases required1536MiB free, ran after the benchmarks, and stopped only the unique R6 project at completion. R6 volumes and the isolated native database are retained. The existing workspace PostgreSQL server was restarted for this work and left available, without resetting prior data.
""",
        "FINAL_GO_NO_GO": f"""ARCHITECTURAL OPTIMIZATION: VERIFIED IMPROVEMENT — scoped to the matched 500-payment result ({p500["elapsed_reduction_percent"]:.2f}% lower median elapsed time), with larger-tier results reported independently.

FINANCIAL CORRECTNESS: PASS — {suite.attrib["tests"]} regression tests, differential financial state and event hashes, independent SQL reconciliation/replay, and controlled negatives.

ORIGINAL 10,000-IN-60-SECONDS TARGET: UNVERIFIED — not attempted because measured 1,000-payment throughput does not justify the attempt.

Keep the measured small architectural changes and the preserved R5 review archive. This is a local synthetic performance improvement, not completion of the original throughput ambition. Maintained lint/format/type gates pass; the broader type inventory retains the same disclosed 41 errors in ten legacy scripts. No universal type-clean claim is made.

The R5 browser acceptance remains historical evidence for the preserved R5 release. Actual R6 Docker payment/restart/host-ingress smoke also passed on 008; 40 runtime/migration source hashes match the tested image. No new browser campaign or Linux throughput result is claimed. The additive008 migration and documented smoke head check are included. Do not infer production readiness, public licensing or external publication from these results. Future performance work must first address the measured native serial command/CPU costs and repeat these financial gates.
""",
    }
    for name, body in reports.items():
        (ROOT / ("docs/THUNDERBOLT_R6_" + name + ".md")).write_text(
            "# THUNDERBOLT-R6 " + name.replace("_", " ") + "\n\n" + body + scope, encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "reports": len(reports),
                "regression_tests": suite.attrib["tests"],
                "optimization": "VERIFIED IMPROVEMENT",
                "financial": "PASS",
                "target": "UNVERIFIED",
            }
        )
    )


if __name__ == "__main__":
    main()
