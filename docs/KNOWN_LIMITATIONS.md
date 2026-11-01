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
  p01_forensics (13), p1b_c2_evidence (8), p1c_d1_evidence (9), p1c_o0_campaign (1),
  p1c_o0_summarize (2), p1c_o1_attribution (2), p1c_o1_campaign (1),
  p1c_o1_diagnose (2), p1c_o1_final_audit (2), p1c_o1_summarize (4).
  The per-file counts above are to be checked against the retained type inventory before release.
  These involve heterogeneous report dictionaries, dynamically imported test modules, and optional
  platform profiling attributes. They are outside the maintained gate; no blanket ignores were
  introduced. Frozen references are retained unchanged. Runtime refactoring to repair old report
  generation is outside this release's scope.
- Legacy campaign/packaging helpers require local historical evidence and archives that are
  deliberately excluded from this public source snapshot. They are historical tools, not quickstart
  commands. Python3.12 is the tested version; other declared versions need separate acceptance.
- Supplied local ingress controls and private-network trust authentication are not production security.
- No ongoing security maintenance SLA or open-source license is granted.
