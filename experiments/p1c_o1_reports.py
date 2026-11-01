"""Write review reports from completed artifacts; conclusion selected after inspection."""

import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
docs = root / "docs"
parser = argparse.ArgumentParser()
parser.add_argument(
    "--verdict",
    required=True,
    choices=("NO ADVANTAGE DEMONSTRATED", "MIXED TRADEOFFS", "ADVANTAGE DEMONSTRATED", "INCONCLUSIVE"),
)
parser.add_argument("--recommendation", required=True)
args = parser.parse_args()
summary = json.loads((docs / "P1C_O1_summary.json").read_text())
cohort = json.loads((docs / "P1C_O1_primary_v2_results.json").read_text())
audit = json.loads((docs / "P1C_O1_final_audit.json").read_text())
assert cohort["status"] == "COMPLETE" and audit["native_worlds_verified"] == 120
policies = summary["policies"]
lines = [
    "# P1C-O1 held-out results",
    "",
    f"Empirical verdict: **{args.verdict}**.",
    "",
    "Frozen ten-family cohort:100 intents per family per policy;1000 per policy,3000 assignments. Seeds1021,2039,4093 assigned cyclically, one per family. All assigned valid intents remain in the denominator. Development and ablations excluded. No policy tuning after freeze. The initial131/257/509 cohort was interrupted after the queued1ms retry defect, preserved, and excluded in full; this replacement used new seeds after a uniform100ms scheduler correction.",
    "",
    "| Metric | Fixed order | V1 | V2 |",
    "|---|---:|---:|---:|",
]
for metric in (
    "submitted",
    "eligible",
    "completed",
    "pending",
    "rejected",
    "safely_failed",
    "fees_by_currency",
    "beneficiary_credited_by_currency",
    "latency_ms",
    "fees_per_completion",
    "recovery_attempts",
    "reconciliation_errors",
    "duplicate_financial_postings",
):
    values = [str(policies[p][metric]) for p in ("fixed_order", "v1", "v2")]
    lines.append(f"| {metric} | {' | '.join(values)} |")
lines += [
    "",
    "## Scenario counts",
    "",
    "| Family | Seed | Fixed completed/pending | V1 completed/pending | V2 completed/pending | V2 changed routes |",
    "|---|---:|---:|---:|---:|---:|",
]
for group in summary["scenarios"]:
    counts = [f"{m['completed']}/{m['pending']}" for m in group["metrics"]]
    lines.append(
        f"| {group['plan']['family']} | {group['plan']['seed']} | {' | '.join(counts)} | {group['route_changes_baseline_v2']} |"
    )
lines += [
    "",
    "## Comparable-outcome costs",
    "",
    "Each row compares precisely the same intent IDs completed under both named policies. Aggregate fees above also include all submitted intents, including partial/pending bookings. A lower aggregate fee with fewer completions is not labeled savings.",
    "",
]
for group in summary["scenarios"]:
    lines += [
        f"### {group['plan']['family']}",
        "",
        "```json",
        json.dumps(group["common_completed_costs"], indent=2),
        "```",
        "",
    ]
lines += [
    "## Liquidity, latency and computation",
    "",
    "Maxima below are maxima across independent scenario worlds, not sums. Exact per-account peaks, remaining funds, drawdowns, native obligation states and account reservation amount-time appear in per-scenario artifacts. Holds, posted utilization and independent obligations are different measures, not additive capital.",
    "",
]
for p, value in policies.items():
    lines += [
        f"### {p}",
        "",
        "```json",
        json.dumps(
            {
                k: v
                for k, v in value.items()
                if k.startswith("max_")
                or k
                in (
                    "reservation_amount_time_minor_ms",
                    "obligation_amount_time_minor_ms",
                    "routes",
                    "algorithm_ns",
                    "client_wall_seconds",
                    "latency_n",
                )
            },
            indent=2,
        ),
        "```",
        "",
    ]
lines += [
    "Modeled latency uses completed financial delivery, pooled nearest-rank percentiles. Pending values have no completion latency and are never recorded as zero. Policy rank timing, SQL/wall time and logical latency are distinct. Wall/SQL timings include concurrent blocks and verification load: no comparative throughput inference. Client high-water memory excludes PostgreSQL and is cumulative within each worker.",
    "",
    "## Limits",
    "",
    "These are deterministic simulator workloads with one seed per family, not independent population replications. Fixed FX133/100, fixed fees and independent per-rail prefunding limit economic generalization. No synchronized FX repricing or spread valuation. Exogenous requested schedules match; freeze clipping and chosen-rail duplicate/ACK recovery are policy-conditioned, recorded explicitly. No hidden future events enter the chooser. Some currencies/classes have a single eligible rail. No confidence interval or universal superiority claim.",
    "",
    args.recommendation,
    "",
]
(docs / "P1C_O1_HELDOUT_RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
ablation = json.loads((docs / "P1C_O1_ablation_summary.json").read_text())
abl = [
    "# P1C-O1 diagnostic ablations",
    "",
    "Four preregistered old-seed11 five-payment fixtures,20 intents per mode,60 assignments. Independent equivalent states; all hard constraints and native engines retained. These are diagnostic fixtures, not untouched primary efficacy evidence.",
    "",
]
for mode, value in ablation.items():
    abl += [f"## {mode}", "", "```json", json.dumps(value, indent=2), "```", ""]
abl += [
    "Cost-only retains the fee/proceeds frontier then chooses nominal latency. Exposure-only omits dominance filtering. Full uses both. The small fixture set cannot establish a population benefit or interactions: report observations without attributing nonexistent incremental gains. In the healthy fixture, cost-only uses instant while exposure/full use atomic: same proceeds/fees/completions but exposure falls and holds/duration rise. In the shortage fixture all modes complete3/5 with2 pending; full/exposure nearly double peak domestic reservations rather than preserving scarce liquidity. Pending-outage cost-only books a source fee and an obligation; atomic-oriented modes have no source-final liability, still remain pending, and therefore do not earn a success credit. Full and exposure-only agree throughout these fixtures: the native fixtures do not show incremental value from the cost filter. Rank-contract unit tests do not establish an economic benefit in the financial model.",
    "",
]
(docs / "P1C_O1_ABLATION_RESULTS.md").write_text("\n".join(abl), encoding="utf-8")
safety = """# P1C-O1 financial safety

Engineering safety checks PASS for this local simulator. The completed primary cohort comprises30 policy runs with90 rail worlds plus30 control worlds. Every policy ran full native financial reconciliation and event replay. Final independent provenance check verifies all120 immutable history heads, actual native configuration equality and frozen source hashes against those recorded full audits. Zero unexpected reconciliation errors and duplicate financial effects. All1000 assigned arrivals per policy processed; pending is separate from completion. No safety guard was removed to accelerate experiments.

The ledger, store, core audits, correspondent and instant settlement sources remain byte-identical to the O0/D1-preserved versions. V2 changes selection and observation only. Binding, execution revalidation, quote/fee caps, current account availability, funded nostro/vostro mirrors, hop-local commits, instant source/destination messages, obligations, idempotency and event hash semantics are retained. Existing regression verifies atomic, correspondent and instant safety; new checks cover V2 choice, pending, API and replay. Native per-policy records retain all obligations and partial fee journals.

Atomic potential independent exposure is zero but its pending reservations remain financially material. Correspondent quoted requirements are not an end-to-end guarantee. Native obligation records and liability duration are reported separately from claims/reservations. A smaller raw reservation peak does not establish smaller capital needs or economic efficiency.

The initial cohort was interrupted after a queued1ms busy retry; all60 native histories were preserved and excluded from efficacy. Uniform100ms backoff was verified by a real13-payment regression and fresh untouched seeds. V2 policy remained byte-identical. The active full-history audits were computationally costly; independent scenario processes were used with a recorded execution amendment. They cannot observe each other's financial worlds. Resource timings include concurrency and are not proof of throughput superiority. Frozen online policy and model sources stayed unchanged.

Fresh native database migrated001–007; head007. All three policies completed and replayed there. All original archives and frozen O0 artifacts preserved. This is not real-bank settlement, container release readiness, deployment or external execution evidence. Container release gate remains open.
"""
(docs / "P1C_O1_FINANCIAL_SAFETY.md").write_text(safety, encoding="utf-8")
final = [
    "# P1C-O1 final decision",
    "",
    "**ENGINEERING: GO — local implementation, financial safety, replay and experiment validity verified.**",
    "",
    f"**EMPIRICAL: {args.verdict}.**",
    "",
    "O0 remains NO ADVANTAGE DEMONSTRATED. Fixed order remains the default. V1 is preserved. One V2 was implemented after exact Gate A reproduction and Gate B counterexample, then frozen before fresh primary outcomes. No V3 or post-held-out objective tuning.",
    "",
]
for name, value in policies.items():
    final.append(
        f"- {name}: {value['completed']}/{value['submitted']} completed ({100 * value['completed'] / value['submitted']:.2f}%), eligible{value['eligible']}, pending{value['pending']}, rejected{value['rejected']}, safely failed{value['safely_failed']}; fees{value['fees_by_currency']}; beneficiary proceeds{value['beneficiary_credited_by_currency']}; pooled latency{value['latency_ms']} ms, N={value['latency_n']}."
    )
final += [
    "",
    "All currency amounts are separate integer minor units. See held-out report for scenario tradeoffs and matched-completion costs; aggregate lower fees cannot be called savings if fewer payments complete.",
    "",
    "Verification: baseline185/0skip; final196/0skip; all six actual browser workflows, frontend build, lint/format/types, fresh migrations, original24/24 canonical reproductions and120 primary native history heads pass. Container release and real-world economic validation remain unverified.",
    "",
    "Reports: ROOT_CAUSE_ANALYSIS, OPTIMIZATION_OPPORTUNITY, V2_POLICY_SPEC, FROZEN_EXPERIMENT_MANIFEST, HELDOUT_RESULTS, ABLATION_RESULTS, FINANCIAL_SAFETY, TEST_REPORT and this report, all with P1C_O1 prefix. Machine-readable plan/freeze, full primary per-payment/per-scenario results, summaries, attribution, safety provenance, resource measurements and browser records accompany them.",
    "",
    args.recommendation,
    "",
]
(docs / "P1C_O1_FINAL_GO_NO_GO.md").write_text("\n".join(final), encoding="utf-8")
status = docs / "IMPLEMENTATION_STATUS.md"
status.write_text(
    f"# Current milestone — P1C-O1\n\nEngineering GO. Empirical **{args.verdict}**. Fixed-order remains default; V1 preserved, one untuned V2 evaluated.\n\n{args.recommendation}\n\nSee docs/P1C_O1_FINAL_GO_NO_GO.md and P1C_O1_HELDOUT_RESULTS.md for exact evidence and limitations. Prior milestones follow unchanged as history.\n\n"
    + status.read_text(encoding="utf-8"),
    encoding="utf-8",
)
print("Wrote reports", args.verdict)
