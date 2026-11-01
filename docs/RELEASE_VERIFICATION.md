# v0.9.0 release verification

Candidate: exact R7 runtime, migrations, scenarios and frontend, with a separate sanitized
publication tree. Internal engine identifier0.1.1 is retained for compatibility. Release tagv0.9.0
identifies the public research snapshot.

## Fresh R8 checks

Local maintained checks: lint PASS, formatting PASS (151 files), maintained mypy PASS
(109 source files). The full historical inventory reproduced41 errors in ten legacy report scripts.
No check was weakened and no runtime financial change was made.

Windows headroom was insufficient for safe clean-build and full regression work. Heavy jobs were
not forced; existing applications, database data and Docker configuration were preserved.
An isolated GitHub Actions runner is selected to run the exact candidate sequentially: clean locked
installation, dependency consistency, maintained quality, full real-PostgreSQL regression, frontend
TypeScript/production build, financial smoke, browser workflows, clean Docker build, recreated-stack
recovery and container browser acceptance. The repository remains PRIVATE during this gate.

Pending remote execution: release correctness UNVERIFIED, Docker UNVERIFIED, public publication
NOT YET PERFORMED. These statements will be replaced only after actual runner evidence is inspected.
Historical R5 and R7 results in the README are explicitly historical and cannot satisfy this gate.

## Independent verdicts

| Acceptance | Current candidate status |
| --- | --- |
| Repository publication | PREPARED, NOT PUBLISHED |
| Exact release-candidate correctness | UNVERIFIED pending isolated runner |
| Docker for selected R7 source | UNVERIFIED pending isolated runner |
| Synthetic research preview | PARTIAL pending release acceptance |
| Original10,000-in-60 | UNVERIFIED |
| Original expanded vision | PARTIAL |

All22 pre-R8 review archives are preserved with recorded SHA256 checksums. All rights reserved.
