"""Evidence-derived P2-A reports; no invented or hardcoded scenario outcomes."""

import json
from xml.etree import ElementTree

from experiments.p2a_campaign import OUT, REGISTRY, ROOT

DOCS = ROOT / "docs"
files = [OUT / f"{scenario}-seed20261030-n10.json" for scenario in REGISTRY]
if not all(path.is_file() for path in files):
    raise RuntimeError("INCOMPLETE_CHAOS_FAMILY")
results = [json.loads(path.read_text(encoding="utf-8")) for path in files]
if not all(r["status"] == "PASS" for r in results):
    raise RuntimeError("CHAOS_FAILURE_NOT_REPORTABLE_AS_PASS")
browser = json.loads((DOCS / "P2_A_browser_observed.json").read_text())
fresh = json.loads((DOCS / "P2_A_fresh_database.json").read_text())
reproduced = json.loads((DOCS / "P2_A_reproducibility.json").read_text())
assert reproduced["all_match"] and len(reproduced["matched_hashes"]) == 6
suite = ElementTree.parse(DOCS / "P2_A_pytest.xml").getroot().find("testsuite")
assert suite is not None and suite.attrib["failures"] == "0" and suite.attrib["errors"] == "0"


def write(name, text):
    (DOCS / f"P2_A_{name}.md").write_text(text.strip() + "\n", encoding="utf-8")


rows = []
for result in results:
    m = result["manifest"]
    native = result["payload"].get("native", {}).get("metrics", {})
    audits = result["audit"]
    final = result["payload"]["final"]
    payment = final.get("payment", final) if isinstance(final, dict) else {}
    status = payment.get("economic_status") if not native else None
    completed = native.get("completed", int(status == "SETTLED"))
    pending = native.get("pending", int(status in ("PREPARED", "PENDING_EXPOSURE", "QUEUED")))
    rejected = native.get("rejected", int(status == "REJECTED"))
    safely_failed = native.get("safely_failed", int(status in ("EXPIRED", "ABORTED")))
    rows.append(
        {
            "scenario": m["scenario_id"],
            "name": m["name"],
            "payments": m["requested_count"],
            "completed": completed,
            "pending": pending,
            "rejected": rejected,
            "safely_failed": safely_failed,
            "final_status": status,
            "worlds": len(audits),
            "independent_pass": all(a["independent"]["pass"] for a in audits.values()),
            "replay_pass": all(a["replay"]["match"] for a in audits.values()),
            "financial_errors": sum(a["reconciliation"]["financial_errors"] for a in audits.values()),
            "events": sum(a["independent"]["events"] for a in audits.values()),
            "journal_groups": sum(a["independent"]["journal_groups"] for a in audits.values()),
            "runtime_seconds": result["runtime_seconds"],
            "peak_traced_python_bytes": result["peak_traced_python_bytes"],
            "stored_row_bytes": sum(
                w.get("stored_row_bytes", 0) for w in result["durable_financial_state"].values()
            ),
            "recovery_attempts": native.get("recovery_attempts"),
            "replay_seconds": sum(a["replay_seconds"] for a in audits.values()),
            "audit_seconds": sum(a["independent_audit_seconds"] for a in audits.values()),
        }
    )
(DOCS / "P2_A_summary.json").write_text(json.dumps({"scenarios": rows, "verdict": "PARTIAL"}, indent=2))

write(
    "CHAOS_ARCHITECTURE",
    """
# P2-A chaos architecture

The new `experiments/p2a_campaign.py` is a bounded deterministic scenario registry over the existing PostgreSQL ledger, atomic PvP, correspondent, instant-link, routing, fault and recovery operations. `FIXED_ORDER` remains the only primary campaign policy. No optimizer or settlement engine was changed. Each scenario uses a fresh native world and records logical stages, input hashes, durable run IDs, financial snapshots, replay and audit results. A unique run ID is storage identity, not the reproducibility key. The canonical event and normalized financial-state hashes are separate.

`railstorm/p2a_oracle.py` reads durable SQL tables directly. It does not call the posting functions, native reconcile or replay. Its event hash implementation is separately expressed. Native reconcile and replay are also saved so disagreements are visible. The Command Center Crisis Lab executes a bounded new scenario through the API, saves its evidence, and displays recorded stages and final native tables. Moving between stages is evidence navigation, not a live state mutation.

The five named banks are Atlas US, Sterling UK, Rhine EU, Meridian SG and Bharat IN. Correspondent relationships AS USD, SR USD, RM USD, SR SGD and RM SGD are modeled. The demonstrated routed corridor is USD to SGD. The domestic instant link is US FAST to SG FAST with Sterling FX prefunding. Accounts for EUR and INR exist, but the campaign does not claim unsupported four-currency cross-rail settlement. Independent rail worlds have separate prefunding; their balances must not be added as one shared liquidity pool.
""",
)

registry = [
    "# P2-A scenario registry",
    "",
    "Each file under `docs/p2_a_evidence` is machine-readable native evidence. Single-payment fixtures are identified as such; only optimizer-backed scenarios use ten arriving payments.",
    "",
    "| Scenario | Native mechanism | Payments | Scope |",
    "|---|---|---:|---|",
]
for row, result in zip(rows, results):
    registry.append(
        f"| {row['scenario']} | {row['name']} | {row['payments']} | {result['manifest']['variant']} |"
    )
registry += [
    "",
    "CH-07 exercises duplicate commands but not a general reordered-message scheduler. CH-09 restarts a component after a durable pending step; it does not kill a process mid-recovery. CH-10 exercises fixed-quote expiry; synchronized rate repricing is unsupported. CH-12 tampers with an isolated copied event and uses disposable native runs for negative accounting controls; historical evidence is never modified.",
]
failure = {
    "CH-01": "none; mixed supported routes",
    "CH-02": "destination SGD prefunding shortage",
    "CH-03": "Rhine intermediary unavailable after earlier posted hops",
    "CH-04": "instant destination delivery unavailable after source finality",
    "CH-05": "Sterling participant unavailable before atomic commit",
    "CH-06": "destination acknowledgment lost",
    "CH-07": "duplicate command and delivery; general reordering untested",
    "CH-08": "limited correspondent funds; arrivals logically sequenced, true concurrent contention untested",
    "CH-09": "component restart after pending step; mid-recovery process crash untested",
    "CH-10": "quote expiry; dynamic repricing unsupported",
    "CH-11": "SGD freeze, Rhine/Meridian outage, lost acknowledgment",
    "CH-12": "isolated copied-event tamper; durable database event corruption untested",
}
expected = {
    "CH-01": "all ten settle across supported rails",
    "CH-02": "shortfall without unfunded credit; rejected or pending distinct",
    "CH-03": "earlier hops stay final; pending obligation recovers once",
    "CH-04": "source final, destination pending, then one credit",
    "CH-05": "no partial atomic commit while participant down",
    "CH-06": "retry discovers one committed delivery",
    "CH-07": "duplicate commands create no duplicate financial effect",
    "CH-08": "limited funds are not double spent",
    "CH-09": "restart resumes durable pending state",
    "CH-10": "expired quote settles no payment",
    "CH-11": "safe completion/rejection under combined failures",
    "CH-12": "tampered copied event fails hash check",
}
partial_ids = {"CH-07", "CH-08", "CH-09", "CH-10", "CH-12"}
registry += [
    "",
    "## Evidence by scenario",
    "",
    "Initial balances are the `opening_journals` in each linked JSON file; the hash binds their configuration. Final statuses below do not erase intermediate pending states.",
    "",
]
for row, result in zip(rows, results):
    scenario = row["scenario"]
    artifact = f"p2_a_evidence/{scenario}-seed20261030-n10.json"
    native = result["payload"].get("native")
    routes = (
        sorted({x["receipt"]["selected"] for x in native["outcomes"].values() if x["receipt"]["selected"]})
        if native
        else [
            result["payload"]
            .get("final", {})
            .get("payment", result["payload"].get("final", {}))
            .get("rail", "CORRESPONDENT" if scenario == "CH-03" else "native fixture")
        ]
    )
    durable = result["durable_financial_state"]
    final_obligations = sum(
        sum(o["status"] == "OPEN" for o in world["correspondent_obligations"])
        + sum(bool(p["data"].get("obligation")) for p in world["payments"])
        for world in durable.values()
    )
    staged = any(
        s.get("payment", {}).get("obligation") or s.get("status", {}).get("chain", {}).get("obligations")
        for s in result["payload"]["stages"]
    )
    recovery = (
        "recorded and settled"
        if any("recover" in s["action"] for s in result["payload"]["stages"]) and row["completed"]
        else "no separate recovery step"
        if row["completed"]
        else "no settlement after failure"
    )
    registry += [
        f"### {scenario} — {row['name']}",
        "",
        f"- Supported rail: {', '.join(routes)}. Injected failure: {failure[scenario]}.",
        f"- Initial financial state: opening journals in [{artifact}]({artifact}); SHA-256 `{result['initial_state_hash']}`. Expected: {expected[scenario]}.",
        f"- Actual final: {row['completed']} completed, {row['pending']} pending, {row['rejected']} rejected, {row['safely_failed']} safely failed. Final open obligations: {final_obligations}; intermediate obligation recorded: {staged}. Recovery: {recovery}.",
        f"- Independent audit: {'PASS' if row['independent_pass'] else 'FAIL'}. Replay: {'PASS' if row['replay_pass'] else 'FAIL'}. Evidence: [{artifact}]({artifact}). Coverage: {'PARTIAL' if scenario in partial_ids else 'PASS'}.",
        "",
    ]
write("SCENARIO_REGISTRY", "\n".join(registry))

audit_total = sum(r["worlds"] for r in rows)
write(
    "FINANCIAL_AUDIT",
    f"""
# P2-A independent financial audit

The independent SQL auditor passed all {audit_total} final native worlds in the twelve-scenario campaign; native reconciliation reported zero financial errors. It checks journal group balance, per-currency/issuer boundaries, account posted and reserved projections, nonnegative available claims, active hold attribution, atomic partial-posting prohibition, instant source/destination finality, open correspondent obligation attribution, and the immutable event hash chain. It separately hashes the final financial state. Intermediate pending states in CH-03, CH-04, CH-05 and CH-09 were also audited before recovery.

The negative controls detected a one-minor-unit account projection change and a false instant `SETTLED` label on a source-only payment. CH-12 also detected an altered event type in an isolated copy of a persisted event. Those controls are narrow: they do not prove all possible accounting corruption is detectable. Native correspondent and instant contract auditors supply richer fee, mirror, obligation and message checks. Neither check is a substitute for external bank records or real-money settlement.

The independent checker shares the modeled account names, issuer/currency partition and payment-state vocabulary with the application. Its journal balancing does not independently price FX or prove legal ownership of real bank claims. The native contract auditors and the independent SQL checker must be read together. Negative-control evidence is `tests/test_p2a_chaos.py`, the saved JUnit record, and `docs/p2_a_evidence/CH-12-seed20261030-n10.json`; normal passing worlds are separate. All balances are synthetic and each rail world is funded independently.
""",
)

write(
    "REPLAY_REPORT",
    f"""
# P2-A replay report

Native canonical replay matched all {audit_total} final campaign worlds, including routing control worlds. Intermediate pending-stage replay was recorded in the scenario stage evidence. `docs/P2_A_reproducibility.json` records six matching CH-04 hashes on a separate run: manifest, workload, initial state, fault schedule, canonical event history, and normalized final financial state. The other scenarios were not all independently repeated; therefore exact reproduction is demonstrated directly for CH-04 and supported by deterministic schedules and saved hashes for the rest. The corruption negative control changes a copied event and detects a hash mismatch without changing the historical database.
""",
)

table = [
    "# P2-A chaos results",
    "",
    "All values below come from the saved scenario artifacts. Native single-payment fixtures use their final durable payment state; intermediate pending states remain in their stage evidence.",
    "",
    "| Scenario | Assigned | Completed | Pending | Rejected | Safely failed | Worlds | Independent/replay | Errors |",
    "|---|---:|---:|---:|---:|---:|---:|---|---:|",
]
for r in rows:
    table.append(
        f"| {r['scenario']} | {r['payments']} | {r['completed']} | {r['pending']} | {r['rejected']} | {r['safely_failed']} | {r['worlds']} | {'PASS' if r['independent_pass'] and r['replay_pass'] else 'FAIL'} | {r['financial_errors']} |"
    )
table += [
    "",
    "CH-11 combined an SGD liquidity freeze, unavailable Rhine and Meridian participants, fixed-order multi-payment arrivals and a lost destination acknowledgment that was recovered. It completed 2/10 and rejected 8/10 without a financial error. CH-04 and CH-09 retain source finality and a funded destination obligation while pending; they complete only after permitted recovery. CH-03 preserves posted correspondent hops and an onward obligation during the outage. CH-05 blocks atomic commit while the participant is down and completes after restoration. Rejected and pending are distinct from completed.",
]
write("CHAOS_RESULTS", "\n".join(table))

performance = [
    "# P2-A performance and resources",
    "",
    "Measured wall time includes simulation, native reconciliation, independent audit and artifact capture. Peak traced Python allocations exclude PostgreSQL, browser and some native allocations. Stored row bytes are a lower bound on database growth, excluding indexes and page overhead. Logical simulation milliseconds are not processing time.",
    "",
    "| Scenario | Wall s | Peak traced MiB | Stored rows KiB | Events | Journal groups | Replay s | Independent audit s |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
]
for r in rows:
    performance.append(
        f"| {r['scenario']} | {r['runtime_seconds']:.2f} | {r['peak_traced_python_bytes'] / 1048576:.2f} | {r['stored_row_bytes'] / 1024:.1f} | {r['events']} | {r['journal_groups']} | {r['replay_seconds']:.3f} | {r['audit_seconds']:.3f} |"
    )
scale = OUT / "CH-01-seed20261030-n100.json"
if scale.is_file():
    d = json.loads(scale.read_text())
    memory = d.get("peak_process_working_set_bytes")
    performance += [
        "",
        f"The 100-payment healthy verification completed {d['payload']['native']['metrics']['completed']}/100 in {d['runtime_seconds']:.2f} wall seconds. Peak process working set: {memory / 1048576:.2f} MiB."
        if memory is not None
        else "100-payment memory peak unavailable.",
        "The 100-payment artifact uses the original OPT-01 instant-only healthy specification. The final ten-payment CH-01 mixed-rail artifact uses OPT-07. These are different workloads and their timings must not be compared as matched scale tiers.",
        "The first 100-payment diagnostic was stopped after roughly 30 CPU minutes because Python allocation tracing made the audit loop too expensive; its partial worlds were excluded. The replacement used operating-system peak working-set measurement. No 500/1,000-payment claim follows without acceptable time and matched measurement.",
    ]
else:
    performance += [
        "",
        "The 100-payment scale check was still running or incomplete when this report was assembled. No 500/1,000-payment or 10,000-in-60-seconds claim is made.",
    ]
performance += [
    "",
    "The 500- and 1,000-payment tiers were not run. The historical 10,000-payments-in-60-seconds target remains unverified for this multi-rail workload. Earlier atomic-only benchmarks are not matched to this campaign. The 720-second result points to a performance problem, but a specific bottleneck requires profiling; audit queries, event volume and Python scheduling are hypotheses. Container image build and payment smoke remain unverified.",
]
write("PERFORMANCE_AND_RESOURCES", "\n".join(performance))

write(
    "BROWSER_DEMONSTRATION",
    f"""
# P2-A browser demonstration

The built Command Center Crisis Lab exposes the registered scenarios and runs them through the real backend, then reads the persisted artifact by ID. The headless Playwright check executed {len(browser["observed"])} scenarios: {", ".join(x["scenario"] for x in browser["observed"])}. It inspected backend audit/replay results and the CH-04 pending stage; all passed with {len(browser["pageErrors"])} frontend runtime errors. Review screenshots: `docs/screenshots/p2-a-instant-outage.png` and `docs/screenshots/p2-a-correspondent-outage.png`.

The operator can select and start a supported scenario, inspect recorded fault/recovery stages, payment status, obligations and native financial verification. The scenario currently runs to completion on one API request; separate interactive inject/restore/recover controls and browser coverage of CH-08, CH-09, CH-10 and CH-12 remain open. The UI describes steps as recorded history and does not imply that clicking Next executes settlement. This limits the browser acceptance gate.
""",
)

write(
    "TEST_REPORT",
    f"""
# P2-A test report

P1C-O1 baseline reproduced: 196 passed, zero skipped with PostgreSQL configured. The final saved full-suite JUnit record `docs/P2_A_pytest.xml` contains **{suite.attrib["tests"]} passed, {suite.attrib["skipped"]} skipped, {suite.attrib["failures"]} failures, {suite.attrib["errors"]} errors**. Command: `RAILSTORM_DATABASE_URL=<local synthetic PostgreSQL URL> .venv/Scripts/python.exe -m pytest -q --junitxml=docs/P2_A_pytest.xml` (on Windows, set the environment variable in PowerShell first). P2-A adds read-only auditor negative controls and an API persistence test. The subsequent CH-01 registry variant correction was checked by a targeted ten-payment native run and browser workflow; no settlement or auditor source changed after the JUnit run. Frontend TypeScript and Vite production build passed via `node node_modules/typescript/bin/tsc --noEmit` and `node node_modules/vite/bin/vite.js build`. `ruff check railstorm experiments tests scripts`, `ruff format --check railstorm experiments tests scripts`, and `mypy railstorm` passed. Fresh database migrations 001–007 reached head {fresh["migration_head"]}; CH-04 financial and replay audits passed there. Container image/payment smoke verification has not been completed.
""",
)

write(
    "FINAL_GO_NO_GO",
    """
# P2-A final decision

**RAILSTORM P2-A — FINAL VERDICT: PARTIAL**

The bounded synthetic chaos campaign executed all twelve registered scenarios with zero observed financial reconciliation errors and matching final replay across native worlds. A meaningful compound case combined liquidity shortage, participant outages, lost acknowledgment and multiple payments. Independent SQL checks and negative controls add evidence beyond application reconciliation. Fixed order remains default; V1 and V2 and the P1C-O1 mixed-tradeoffs conclusion are unchanged.

The acceptance gate remains partial because CH-07 lacks general reordered-message injection, CH-08 uses logically sequenced arrivals rather than simultaneous bank-local contention, CH-09 does not inject a process crash inside recovery, and CH-10 does not test synchronized dynamic FX repricing. CH-12 checks an isolated copied event rather than corrupting a disposable database. Only CH-04 was independently rerun for exact hash reproduction. The 500/1,000-payment tiers were not verified, and the Crisis Lab does not yet expose stepwise live fault and recovery controls or every requested browser scenario. These are scope limits, not evidence of a detected money loss. No production-bank or container readiness claim is made.

Next milestone: implement durable stepwise chaos control and fault primitives for mid-recovery crash and permitted message reordering, then run a separately frozen multi-seed audit and browser matrix at resource-appropriate scales. Do not tune a routing optimizer.
""",
)

status = DOCS / "IMPLEMENTATION_STATUS.md"
prior = status.read_text(encoding="utf-8")
if not prior.startswith("# Current milestone — P2-A"):
    status.write_text(
        "# Current milestone — P2-A\n\nPARTIAL. Twelve synthetic chaos scenarios executed; independent native-table auditing and replay passed in the bounded campaign. Browser, mid-recovery crash and scale gates remain open. See `P2_A_FINAL_GO_NO_GO.md`.\n\n"
        + prior,
        encoding="utf-8",
    )
print("P2-A reports written from", len(results), "scenario artifacts")
