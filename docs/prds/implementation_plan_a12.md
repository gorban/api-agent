# Test-picker responsiveness

[Python TUI](implementation_plan_a.md)

The integration/E2E picker must remain responsive while navigating rows and
changing individual, file, feature or whole-suite selections. Preserve exact
selected-test configuration, account controls and historical cost estimates.
The same responsiveness requirements apply to Review configuration, including
selected-test review, row navigation and edits. Treat substantial delays in
these interactions as a regression requiring repair.

- [ ] Measure steady row movement, individual/group toggles, initial entry and configuration-review return with representative selected workflow/tool declarations. Separate event-loop work, rendering, declaration discovery, configuration composition and historical storage access; record unprofiled latency alongside profiler attribution.
- [x] Reuse parsed definitions within a bounded configuration session, rechecking captured bytes and lexical paths, including explicit replacements, on each reuse. Reduce repeated resolution within review-row refreshes and account availability checks. Keep effective settings and selected-tool composition fresh; use no persistent cache.
- [x] Preserve freshness after declaration, implementation selector, account, environment or saved configuration changes. Start continues to validate the current selection and effective inputs; reuse must not authorize stale configuration or execution. Verify missing/invalid resources, retargeted links, installed ownership changes and bounded cache eviction with deterministic regressions.
- [ ] Preserve overlapping group toggles, keyboard ranges/search, copy, selected-test review, conditional fields, error recovery and cost rollups. Retain normal configuration precedence and read-only discovery without native imports or prerequisite startup.
- [ ] Compare before/after unprofiled interaction distributions on the same dataset and agree a user-facing latency budget. Verify freshness and isolation regressions, applicable coverage and independent/external review before human acceptance. A documented no-change decision needs evidence that the measured delay is immaterial.
- [ ] Human acceptance of navigation and selection responsiveness with the normal integration list and an extended resource-pack catalog.

## Delivered regression repair

The bounded repair covers both the test picker and selected-test configuration
review. Existing selection/conflict, account and configuration-refresh checks
pass. Independent external review closed the replacement-link invalidation
finding and reported no residual in the repaired scope. Broader performance
sampling, an agreed latency budget, remaining commit gates and human acceptance
stay open above.
