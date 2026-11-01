# Known limitations

- Synthetic local research only; no real banks, markets, accounts or money. Not production ready.
- Five institutions and four currency definitions do not imply all corridors or rails are funded.
- Fixed-order routing is the default; experimental policies have no universal advantage. Native
  dynamic-market instant execution is not advertised through the shared router.
- Same-world writes serialize through the run lock. The prepared-command result facade covers
  internal application use, not the full third-party SQLAlchemy Result/ExecutionContext API.
- 10,000 payments within60s remains UNVERIFIED. Best historical R7 native1,000 rate60.32/s.
  R4/R7 are not controlled cross-version comparisons. Audit/replay timing is separate.
- Full expanded flagship vision remains PARTIAL. No new optimization campaign is included.
- Historical R5 Docker/browser results and R7 native results are distinct from fresh release checks.
- The historical generator inventory has41 mypy errors in10 scripts. Reproduced during R8 review:
  See [the exact Windows inventory](evidence/legacy-type-inventory-windows.json). The fresh Linux
  inventory has52 errors in12 historical scripts, including unguarded Windows profiling APIs;
  see [the complete Linux diagnostics](evidence/legacy-type-inventory-linux.txt).
  These involve heterogeneous report dictionaries, dynamically imported test modules, and optional
  platform profiling attributes. They are outside the maintained gate; no blanket ignores were
  introduced. Frozen references are retained unchanged. Runtime refactoring to repair old report
  generation is outside this release's scope.
- Legacy campaign/packaging helpers require local historical evidence and archives that are
  deliberately excluded from this public source snapshot. They are historical tools, not quickstart
  commands. Python3.12 is the tested version; other declared versions need separate acceptance.
- Supplied local ingress controls and private-network trust authentication are not production security.
- No ongoing security maintenance SLA or open-source license is granted.
