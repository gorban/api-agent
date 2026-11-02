# Shared repository-preview checks

[Envelope compatibility](implementation_plan_b1a3a.md) · [Hook and CI checker](implementation_plan_i1a.md)

- [x] Independently parse the raw v2 carrier prefix with CommonMark and require every reserved bootstrap label to be consumed as a link definition. Check root help as well as component/node bootstrap, without rejecting literal examples in executable Markdown. Preserve runtime legacy-envelope compatibility.
- [x] Apply this static check to every admitted document in the existing recursive application/test view inventory, including explicitly mapped owners. The same `tools/checks.py` path must reject malformed output even when generator and saved files agree. Existing freshness, staging, safe-file and ownership checks remain enforced.
- [x] Pre-commit checks the index; CI checks a supplied snapshot using `--head --root .`, falling back to actual files when Git metadata is unavailable, or explicit `--no-git --root .`. Keep checks read-only, bounded and offline: no inference, browser, renderer, provider or application startup.
- [x] Prove generated-but-malformed rejection, valid root/nested/component envelopes, body examples, legacy inputs, staged-snapshot and no-Git parity. Complete applicable coverage, causal controls and independent external review. CI workflow/required branch-protection wiring remains under the broader managed-check requirements.

Verification: the canonical launcher passed 23,630 unit cases with 100% line and branch coverage. A new-flow discovery regression added after collection passed separately with all ten focused preview cases. A causal guard-removal control and real staged/no-Git canaries rejected malformed output even when generation and saved files agreed. Independent source and public-boundary review found no actionable issue. CI workflow and required-status wiring remain future work.
