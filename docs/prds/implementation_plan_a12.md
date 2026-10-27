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

## Collection transitions and selected text

- [x] Keep an open Quit confirmation visible from queueing through collection and inspection cleanup, preserving its typed response and focus when collection succeeds or fails. Cancel must reveal the collected picker or error; confirming Quit must cancel delivery and prevent automatic test startup.
- [x] Keep mouse text selection, contextual copying and subsequent keyboard navigation responsive. Selection and focus changes must not repeatedly resolve selected-test configuration or rebuild unchanged shortcut hints; account actions and Start must still use current authoritative configuration.
- [x] Verify collection success/error/cancellation, pending automatic start and repeated selection/copy/navigation, measure the warm selected-text path, and complete independent/external review.
- [ ] Human acceptance of confirmation persistence and responsiveness after selecting picker text.

## Clear configuration navigation

- [x] Keep Clear .env cursor handlers and shortcut checks free of configuration resolution and filesystem reads. Use the displayed presence snapshot for shortcut availability; refresh it on entry, explicit refresh and after deletion. Coalesce navigation-triggered discovery in an isolated background resolver and reject obsolete or changed-source snapshots before delivery. Retain bounded source-receipt validation immediately before applying a display snapshot; this delivery check may read sources synchronously, but must not rediscover or resolve configuration. Keep help declarations in the same display snapshot without replacing execution authority. Deletion must apply to current assignments in every target file, including external edits since display.
- [x] Measure navigation after three seconds idle and verify retained rows, effective-source independence, external refresh and deletion freshness; complete independent review before acceptance.

## Idle and account-switch regression

- [x] Profile ordinary integration selection at three and six seconds after screen entry, including navigation, toggles, footer/focus updates and unattended-index switching. Use current configuration and representative local history; report unprofiled latency separately from profiler overhead.
- [x] Keep local history validation and cost-estimator reconstruction off the UI event loop when switching account selection. Coalesce obsolete work, apply only results matching the active visit/configuration/account, preserve selected-test profiles and authoritative Start validation, and retain safe ownership through cancellation or exit.
- [x] Verify rapid index changes, stale deliveries, resolver failures, history changes and subsequent navigation with deterministic and causal checks. Complete independent/external source review.
- [x] Remeasure the same three/six-second interactions in the controlled 18-test catalog with its current history. In the repeated-account/key phase, maximum event-loop pauses fell from 3.769/3.742 seconds to 226/224 milliseconds. The separate unchanged-configuration synchronization phase measured maximum pauses of 309/298 milliseconds with a bounded synthetic empty remote source. These controlled catalog/history and transport measurements leave broader sampling, an agreed latency budget and human acceptance open under the [TUI requirements](implementation_plan_a.md) and above.
- [ ] Human acceptance of sustained responsiveness and account switching.
