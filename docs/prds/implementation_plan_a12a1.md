# Warm selection and regex-search actions

[Large-suite interactive selection](implementation_plan_a12a.md)

## Approved virtualized picker

- [x] Obtain human approval for a shared virtualized picker across unit, integration and E2E selection. Earlier warm-navigation acceptance does not establish group-toggle or search responsiveness.
- [x] Prevent sparse retained selection storage after bulk deselection from inflating intersection work. Preserve exact membership, selection references and group intent; verify equal logical results before and after compaction.
- [x] Build immutable file/tag/test membership indexes once per admitted collected catalog. Rebuild them on recollection; never reuse old identities or change overlapping group unions, selected scopes or exact exclusions.
- [x] Use one fresh selection-row snapshot per action. Resolve selected counts from the authoritative selection without repeatedly reconstructing group memberships. Preserve direct selection changes, account/configuration validity, costs and fresh execution admission.
- [x] Render only visible picker rows using a true virtual scrolling view backed by the global collection and search projection. Keep global identity, cursor, selection and range semantics across the full list; preserve unit, integration and E2E metadata, configuration and cost behavior. Avoid eager construction or Rich sizing of offscreen cells and costs; cache bounded label-width metadata once per distinct catalog label.
- [x] Preserve full-identity regex matching, invalid-pattern behavior, direction/cursor placement, ranges, selected tests outside matches, search-group toggles, complete copy and return-to-selection behavior. If later measurements still require deferred or coalesced search, specify generation/admission and cancellation behavior before adding it.
- [x] Compare unprofiled actions and profiler attribution on the same real catalog after three, six and longer idle periods. Measure All-off, search-bar availability and each query edit separately, with event-loop lag, CPU and memory. Report headless settlement separately from terminal display opportunity; a few samples do not establish p95.
- [x] Evaluate the earlier proposed sub-500 ms group-toggle target, and propose search/input budgets from measurements. Preserve warm row navigation. Do not declare these targets achieved before verification.
- [x] Verify exact selection/search/configuration invariants, meaningful causal controls, applicable full coverage, source/installed behavior and independent source review.
- [x] Complete final attribution/public-boundary and external PR-style review.
- [x] Obtain human acceptance for warm All-off, regex entry and typing, far scrolling, range selection and full-value copy; repeat selected-test configuration/account and cost behavior in integration or E2E.

## Verification

On 21,845 real collected unit cases, three headless settled-action samples after three/six/thirty seconds idle reduced All-off from 13.06–13.20s to 0.243–0.347s, search opening from 11.53–11.75s to 0.274–0.396s and individual query edits from 5.53–11.26s to 0.118–0.397s. Peak RSS was 149.3 MiB. These samples are not p95 estimates or physical display latency; observed action times were below 500 ms, without establishing a general percentile guarantee.

A separate final-source PTY probe measured 100 warm movements at each of the top, middle and end, paired with three/six/thirty seconds idle. Nearest-rank p95 was 17.51/17.38/17.39ms; entry took 2.90 s. The marker follows cursor rendering and terminal output availability, not physical pixels. A real 18-case integration control with history unavailable opened selected-test configuration in 0.646 s and returned in 0.556 s. The base checkout has no bundled E2E suite; synthetic UI probes verify shared E2E behavior.

The exact full unit launcher passed 21,852 cases with 100% lines and branches across 325 production files. Independent native UI probes and six causal controls verify global identity/events, offscreen work bounds, copy, selection compaction, ranges and distinct Unicode previews. Two permission fixtures now explicitly create their intended insecure mode, independent of caller umask; the direct screen fixture uses the current row model without changing its assertions.

Textual event-dispatch adaptations retain the upstream [MIT notice](../../src/api_agent/tui/LICENSE-Textual.txt). Source and wheel distributions must retain it. Attribution, boundary and external review are complete. Human review approved the interactive test-selection improvements.

The picker adapter depends on Textual's row and location metadata and its visible-row rendering path; it uses fixed-height rows and fixed base columns, with an optional final cost column. The dependency is pinned to verified Textual `8.2.8`. Any upgrade must rerun the native event, viewport, range, Unicode-label and cost-snapshot contract checks plus the applicable full gate before changing the pin; no unverified private layout is admitted by a broad version range.
