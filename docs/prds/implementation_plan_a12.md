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
sampling, an agreed latency budget and human acceptance stay open above.

## Rapid navigation follow-up

- [x] After at least three seconds idle, measure queued up/up and down/down input in the collected test picker and all-selected test configuration review. Distinguish input delivery, cursor changes and actual display opportunities; do not treat a headless compositor callback as a captured terminal frame.
- [x] Keep configuration discovery off the cursor event path. Use one isolated read-only display resolver with coalesced requests; discard obsolete results after later navigation, configuration changes or screen transitions. Display values, source and saved/default actions from one source snapshot. Explicit editing and Start retain fresh authoritative resolution.
- [x] Verify external source changes during pending work, modal/screen transitions, failed refresh recovery, independent resolver ownership and burst navigation and complete independent/external review before requesting human acceptance.

- [x] Before delivering a background review snapshot, recheck its captured persisted configuration and selected declaration content. Retry intervening edits or removals without requiring another navigation event; bound repeated unstable reads and offer a visible retry path. Keep stale values and saved/default actions from being applied together after a detected source change.

- [x] Record read receipts in the existing declaration/workflow owners. Include contributed schemas, dormant inventory reads, lexical link targets, optional or unavailable inputs and package ownership/inventory observations used by review. Keep receipts context-local, immutable when delivered and separate from execution authority; preserve the ordinary readers and their errors outside display capture.

## Picker exit feedback

- [x] Retain actual background read completion after cancelling its UI waiter. Stop unnecessary cost-history synchronization when returning to the menu or quitting, preserving already cached records and existing transport timeout/retry limits. An in-flight SDK call may exceed the logical synchronization budget; do not promise immediate interruption or shell return. Preserve context-local execution metadata in threaded readers; never detach work still writing the history cache.
- [x] After the terminal display is restored, identify pending background work with fixed descriptive labels, flush the message immediately and report completion. Keep quick exits quiet and avoid repetitive status spam or raw storage diagnostics. Await owned readers without shutting down another caller's executor.
- [x] Verify blocked reads, cancellation, late failures, fast exits, menu return/reentry and exception paths, with independent/external review before human acceptance. Record actual exit-phase timing separately from startup and navigation.
- [ ] Human acceptance of visible shutdown feedback and return of shell control after leaving the integration/E2E picker without running tests.
