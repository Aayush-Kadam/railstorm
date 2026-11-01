"""Assert complete measured evidence and generate the P0.1 decision report."""

import hashlib
import json
from pathlib import Path

reports = Path("experiments/reports/p01")


def save(path, content):
    target = Path(path)
    scratch = Path("work/p01-reports")
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = scratch / (target.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


base = json.loads((reports / "baseline-verified-10000.json").read_text())
final = json.loads((reports / "final-safety-10000.json").read_text())
browser = json.loads(Path("docs/p01-browser-observed.json").read_text())
assert browser["passed"] == 1 and browser["engine"] == "0.1.1" and browser["pageErrors"] == []
assert browser["committedWebSocketFrames"] > 0 and browser["settledFrames"] > 0
for count in (100, 1000, 5000, 10000):
    original = json.loads((reports / f"baseline-verified-{count}.json").read_text())
    candidates = [json.loads((reports / f"final-{count}.json").read_text())]
    if count == 1000:
        candidates += [json.loads((reports / f"opt{i}-1000.json").read_text()) for i in (1, 2, 3)]
    if count == 10000:
        candidates += [final, json.loads((reports / "final-repeat-10000.json").read_text())]
    for candidate in [original, *candidates]:
        assert candidate["scenario"] == original["scenario"]
        assert candidate["independent_settled_count"] == count
        assert candidate["independent_invalid_leg_group_count"] == 0
        assert candidate["independent_unbalanced_count"] == 0
        assert candidate["reconciliation"]["financial_errors"] == 0
        assert candidate["replay"]["match"]
        assert candidate["replay"]["state_hash"] == original["replay"]["state_hash"]
        assert candidate["replay"]["event_hash"] == original["replay"]["event_hash"]
        assert candidate["row_counts"]["events"] == candidate["row_counts"]["outbox"] == 14 * count + 34
        assert candidate["row_counts"]["journal_entries"] == 2 * count + 4
        assert candidate["row_counts"]["journal_lines"] == 5 * count + 8
        assert candidate["row_counts"]["reservations"] == 2 * count
        assert candidate["independent_currency_totals_minor"] == original["independent_currency_totals_minor"]
for name, expected in final["source_sha256"].items():
    assert hashlib.sha256((Path("railstorm") / name).read_bytes()).hexdigest() == expected

seconds = final["execution_seconds"]
speedup = base["execution_seconds"] / seconds
ram = final["whole_process_memory_after_audits"]["client_peak_rss_bytes"] / 2**20
db = final["sql_and_transactions"]["execution"]
verdict = "PARTIAL"  # Performance/deployment gates explicitly remain unresolved.
summary = {
    "verdict": verdict,
    "baseline_seconds": base["execution_seconds"],
    "final_seconds": seconds,
    "historical_reported_seconds": 470.70934560000023,
    "speedup_vs_reproduced_baseline": speedup,
    "target_60_seconds_achieved": seconds <= 60,
    "financial_errors": 0,
    "duplicate_or_partial_leg_groups": 0,
    "replay_mismatches": 0,
    "test_count": 60,
    "docker": "BLOCKED — ENVIRONMENT",
    "final_source_sha256": final["source_sha256"],
    "row_counts": final["row_counts"],
}
save(reports / "acceptance.json", json.dumps(summary, indent=2))
document = f"""# P0.1 final decision: {verdict}

**Financial foundation verified within the shared-book model; performance and Docker gates remain open. P1A was not started.**

## Acceptance table

| Metric | Historical report | Reproduced unchanged baseline | Final exact-source run |
|---|---:|---:|---:|
| 10K engine runtime | 470.709346s | {base["execution_seconds"]:.6f}s | {seconds:.6f}s |
| Payments/second | 21.24 | {base["payments_per_second"]:.2f} | {final["payments_per_second"]:.2f} |
| Speedup versus today's unchanged baseline | Not comparable | 1.00× | {speedup:.2f}× |
| Financial invariant errors | 0 reported | 0 independently checked | 0, including stronger quote/hold/fee audit |
| Duplicate / partial leg groups | 0 reported | 0 | 0 |
| Replay mismatches | 0 reported | 0 | 0; original event and state hashes retained |
| Events / outbox rows | 140,034 retained | 140,034 / 140,034 | 140,034 / 140,034 |
| Journal headers / lines | 20,004 / 50,008 retained | 20,004 / 50,008 | 20,004 / 50,008 |
| Client process peak through full audits | Unknown | {base["whole_process_memory_after_audits"]["client_peak_rss_bytes"] / 2**20:.2f} MiB (multi-volume process) | {ram:.2f} MiB (fresh 10K process) |

**The 60-second target was not achieved.** Final engine + required reconciliation (including its complete replay): **{seconds + final["reconciliation_including_replay_seconds"]:.6f}s**. Standalone replay is an additional independently timed **{final["standalone_replay_seconds"]:.6f}s**; reconciliation including another replay took **{final["reconciliation_including_replay_seconds"]:.6f}s**. No correctness work disappeared from the measured acceptance.

## Repetition / environment / measurement honesty

The first hardened series measured 168.133939s at 10K; the fresh-process repeat before the final rollback-only guard measured 152.059842s. The final exact-source result above includes that guard and local version 0.1.1; all accepted healthy results have identical financial/event hashes and row counts. Those earlier two timings are retained rather than overwritten. Timing variation is visible and there is no confidence-interval claim from this small sample.

Native localhost PostgreSQL 18.6, fsync/synchronous_commit on, Python 3.12.10, Intel i7-13620H, 16 logical CPUs, approximately 15.70 GiB physical RAM. Database population grows as evidence is preserved. The final single-volume process avoids attributing previous-volume audit memory to its engine. Memory is client working set, not all PostgreSQL processes/shared memory. Whole-system CPU includes unrelated workloads; isolated server CPU remains unmeasured. See PERFORMANCE_BASELINE for exact scopes and original timing limitations.

Final engine client CPU: **{final["client_cpu_seconds"]:.6f}s**, **{final["client_mean_cpu_one_core_percent"]:.2f}% of one core** averaged over execution. Whole-system mean CPU across all workloads: **{final["system_mean_cpu_percent_all_workloads"]:.2f}%**. Engine cursor calls: **{db["sql_count"]:,}**; logical parameterized statements: **{db["logical_sql_statement_count"]:,}**. These are different metrics: batching preserved every event/outbox row. Commits / advisory-connection rollbacks: **{db["commit_count"]} / {db["rollback_count"]}**; mean / max transaction lifetime **{db["mean_transaction_seconds"]:.6f}s / {db["max_transaction_seconds"]:.6f}s**, including overlapping guard transactions. Zero SQLSTATE errors/conflicts or retryable database retries in the healthy fixture.

## What changed

Three measured optimizations only: final queue/time bookkeeping and transaction-local fault view; frozen event/outbox batching before the existing commit; deep-copy transaction-local payment read cache. Safety hardening additionally checks exact holds and immutable quote/command, independently audits journal deltas/fees, uses a consistent reconciliation snapshot, makes initialization atomic, supports idempotent prepared abort and marks a caught nested command failure rollback-only. No concurrency, rail, router or decorative feature was added. No constraint/trigger, financial event, settlement identity or final audit was disabled.

## Validation

`python -m pytest tests -q`: **60 passed**, no skips, one upstream deprecation warning. Ruff lint/format and mypy (10 modules) passed. Strict TypeScript/production build passed with the existing bundle-size warning. Native browser regression against engine 0.1.1 passed: zero page errors, {browser["committedWebSocketFrames"]} actual WebSocket frames including {browser["settledFrames"]} frames containing committed SETTLED, scenario/step/run/journal/replay/reconcile/export/fault/reload/mobile/keyboard/reduced-motion checks. Original 37 tests were reproduced before edits. Two new guard tests failed on the old implementation before their fixes; they are not hidden. The added browser health-version assertion initially used a relative request URL without a base; that test URL was corrected and the workflow rerun. Exact campaign is in CORRECTNESS_REGRESSION.

## Docker and remaining risks

**Docker: BLOCKED — ENVIRONMENT.** The Linux engine pipe is absent. No image build/start or container persistence/replay pass is claimed. Native test evidence does not substitute for Docker verification. Remote CI remains unobserved.

The prototype remains a synthetic, local, single shared issuer ledger with simplified operational faults. No full abandoned-hold recovery daemon, authentication/authorization, HA, power-loss proof, arbitrary network partition model or independent rail finality has been established. Actual subprocess-exit tests now cover DB disconnect rollback and durable post-commit recovery, but not hardware/disk failure. PostgreSQL owner/admin can disable triggers. Full audit still materializes history and has a significant memory cost. Three bounded performance changes are complete; further redesign is deferred rather than weakening safety to hit a timing number.

## Decision and next precise milestone

**PARTIAL** is the defensible verdict: monetary safety evidence improved and measured runtime improved without missing effects, but 60-second performance and container reproducibility remain unresolved. This is not a release or full flagship GO.

**Next milestone: P1A — correspondent banking rail**, only after separate authorization and explicit acceptance/closure of the remaining gates. Begin with an independently committed intermediary source leg, durable target obligation and explicit exposure under intermediary outage; preserve all P0.1 golden/concurrency/crash/replay gates. No P1A implementation occurred during this sprint.
"""
save("docs/P0_1_FINAL_GO_NO_GO.md", document)
original_status = Path("docs/archive/P0_IMPLEMENTATION_STATUS.md").read_text(encoding="utf-8")
status = f"""# Current checkpoint — P0.1 / local package 0.1.1

**Verdict: PARTIAL.** 60 automated tests passed; native browser regression passed. Exact final 10K engine time **{seconds:.6f}s**, versus reproduced unchanged **{base["execution_seconds"]:.6f}s** ({speedup:.2f}×). Target ≤60s remains unmet. Full financial/event counts and hashes match. Docker is BLOCKED — ENVIRONMENT; remote CI unverified.

See [final decision](P0_1_FINAL_GO_NO_GO.md), [foundation audit](P0_1_FOUNDATION_AUDIT.md), [baseline](P0_1_PERFORMANCE_BASELINE.md), [profile](P0_1_PROFILING_REPORT.md), [optimization log](P0_1_OPTIMIZATION_LOG.md), [regression campaign](P0_1_CORRECTNESS_REGRESSION.md) and [Docker status](P0_1_DOCKER_VERIFICATION.md).

P0.1 corrected missing/wrong reservation and quote projection guards, independent fee/journal audit, initialization atomicity, consistent audit snapshots and caught nested-command rollback behavior. Only three measured performance changes were made. Original P0 evidence/archive is preserved; source snapshots match measured hashes. No new rails/routing/frontend scope was added. P1A is the next separately authorized milestone and has not started.

---

## Historical P0 checkpoint (retained context; current measurements above supersede timing/test counts)

"""
save("docs/IMPLEMENTATION_STATUS.md", status + original_status)
readme = Path("docs/archive/P0_README.md").read_text(encoding="utf-8")
save(
    "README.md",
    readme.replace(
        "# RAILSTORM\n",
        f"# RAILSTORM\n\nCurrent patch: **P0.1 / 0.1.1 — PARTIAL**. 60 tests and native browser regression passed. Final 10K: **{seconds:.2f}s**, **{speedup:.2f}×** faster than the reproduced unchanged baseline; the 60s target remains unmet. Docker is environment-blocked. [Decision and exact evidence](docs/P0_1_FINAL_GO_NO_GO.md). Original P0 results below remain historical evidence.\n",
        1,
    ),
)
print(json.dumps(summary, indent=2))
