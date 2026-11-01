# v0.9.0 release verification

Selected baseline: **R7**, preserving the exact financial runtime, migrations, scenarios and
frontend. The R5 fallback is unnecessary: R7 has now passed fresh container acceptance.
Internal engine identifier 0.1.1 remains for compatibility; v0.9.0 identifies the research release.

## Fresh acceptance

[Passing application and container checks](https://github.com/Aayush-Kadam/railstorm/actions/runs/41342297413)
tested commit `a22be7fa09d2d9af0d39ee28e70c3aa2e26bd25a`. [Machine-readable summary](evidence/release-acceptance.json).

| Check | Actual result |
| --- | --- |
| Clean locked Python install and dependency consistency | PASS |
| Backend lint / formatting / maintained types | PASS; 151 formatted files, 109 maintained typed files |
| Full real-PostgreSQL regression | **268 passed**, 0 failures, 0 errors, 0 skips; 94.46s |
| Frontend locked install / TypeScript / production build | PASS |
| Native all-rail financial smoke | PASS |
| Ten native browser suites | PASS |
| Clean Docker image build / startup / health | PASS |
| Docker payment smoke and independent financial checks | PASS |
| Recreated application + database containers, durable state and recovery | PASS |
| Live Crisis Lab browser against actual container image | PASS |

This is **CI-hosted Docker execution**, not a fresh local Windows Docker result. Local heavy
verification was resource-blocked and was not forced. No safety threshold was lowered; no
essential process, shared database or historical archive was removed. Checks run sequentially
in an isolated runner. Final release workflow also records explicit 2GiB RAM / 3GiB disk gates.

The container smoke preserves source-final/destination-pending 13,300 minor units over database
recreation, then restores/retries with one beneficiary credit and two journal groups. Native SQL
audit, independent audit and replay pass. Raw [container smoke](evidence/release-container-smoke.json),
[native smoke](evidence/release-native-smoke.json), [pytest XML](evidence/release-pytest.xml), and
[image identities](evidence/release-container-images.json) are retained. The downloaded artifact
SHA256 was independently checked against GitHub's recorded digest before using its contents.

## Failed preparation attempts retained

Run 1 found two Linux type issues in Windows-only diagnostic helpers; explicit platform guards
fixed them without changing the financial engine. Run 2 exposed the actual Linux legacy inventory
(52 errors in 12 scripts versus Windows 41 in 10); both inventories are disclosed. Run 3 passed 268
tests and frontend build but its Ubuntu 26.04 browser installer was unsupported by pinned
Playwright 1.56.1. Pinning Ubuntu 24.04 resolved the environment mismatch. No failed result was
relabeled as passing; no test or financial assertion was removed.

Run 4 passed every application, financial, browser and Docker step and uploaded its evidence,
then the job failed in optional uv cache pruning after a 300-second lock timeout. Its overall
workflow is therefore **FAIL**, despite the separately passing acceptance steps above. The final
workflow disables optional cache persistence and starts the installed uvicorn directly, avoiding
a long-lived uv wrapper. Run 5 was canceled as superseded. The final release requires a completely
successful workflow, including cleanup; these failed/canceled runs remain visible in Actions.

## Separate verdicts

| Acceptance | Verdict |
| --- | --- |
| Public publication gate | GO after final workflow passes; remote visibility verified separately |
| R7 release-candidate correctness | PASS |
| Docker for selected R7 source | PASS, isolated CI-hosted execution |
| Synthetic research preview | GO for tested local synthetic workflows |
| Original 10,000-in-60 | UNVERIFIED |
| Original expanded vision | PARTIAL |

Publication documentation, pinned CI actions, resource probes and fresh screenshots are finalized
after this accepted source revision; the final public commit must preserve the verified runtime
fingerprints and pass its own workflow. All rights reserved. All 22 pre-R8 archives remain unchanged.

This report records source acceptance. The repository's public visibility and the v0.9.0 tag are
checked separately at publication; the tag identifies the final release commit.
