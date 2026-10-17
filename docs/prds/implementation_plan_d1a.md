# Selected-test Allure viewer

[Selected-run viewers](implementation_plan_d1.md) · [Reporting lifecycle](implementation_plan_h2a2.md) · [Original UI evidence](implementation_plan_a4a.md)

## Navigation and availability

- [x] Add `a View in Allure` to integration/E2E result tables for exactly one
  highlighted individual test with a terminal outcome, whether passed, failed,
  errored or skipped. Support both live results while other tests run and the
  completed-results screen. Hide and disable it for a multi-row/range selection,
  file/category/aggregate rows, pending/running tests and unit tests. Respect
  text-entry focus; this is not a test-selection shortcut.
- [x] Open the selected test's exact Allure result/attempt directly, retaining
  suite/run identity. Do not substitute the newest run or a similarly named test.
  Parameterized tests and retries need distinct, verified result mappings.
- [x] Allow repeated navigation between completed tests without disturbing their
  original DEBUG browsers, test selection, active execution or suite exclusion.
  Reuse the dedicated menu viewer backend, which also supports browsing multiple
  finalized integration/E2E runs from the main menu. The shortcut still targets
  only its exact selected test/attempt.

## Early results and serving

- [x] Make a completed test and its captured attachments viewable before suite
  completion. Produce an operation-owned, validated local preview if final run
  publication is not yet available. Collect attachment sources recursively through nested steps,
  retaining the same flat-name, hash, count and byte limits. Preserve outcome/teardown ordering and exact
  attachment bytes; never publish a partial finalized manifest or combine runs.
  Bind each versioned preview to the run, test/attempt and validated snapshot
  artifact digests. Later tests or final publication require an explicit new
  route or controlled rebind preserving the selected attempt; never silently
  change a previously verified binding. Label pending suite-wide accounting
  honestly. Final immutable history remains authoritative and independent of
  viewer generation or failure.
- [x] Generate/serve under bounded shared progress and readiness, then open a
  managed headed browser at the selected result. Display a usable URL and safe
  failure diagnostics. If local results are absent, stage the exact finalized
  S3 run in owned temporary storage with verified manifests/hashes.
- [x] Retain viewer server, staging and browser while the test-results operation
  is active, including after suite completion. Dispose through existing owned
  cleanup when canceling/leaving results, returning to selection or quitting. Preserve pending cleanup and retry duties.
  Browser closure alone must not cancel tests. Never stop shared reporting
  storage or another owner's viewer merely because this viewer closes.
- [x] Reuse only an exact verified viewer binding, or explicit separate run
  routes. Handle foreign/different-run port conflicts clearly; no silent report
  replacement, latest-run substitution or unapproved port switching.

## History in selected reports

- [x] Include native per-test History in exact selected-attempt reports, including a completed test while its suite is still running. Use the configured working-results/S3 or secondary-only history source. Preserve the exact current snapshot and a single current result; never pool independent launches as retries or replace it with a stored result.
- [x] Transfer at most ALLURE_HISTORY_DISPLAY_COUNT minus one earlier finalized launches of the same repository/mode (default 19), including the current run in the configured window. Exclude the current run ID and launches ending after the selected run started. Keep parameterized identities distinct; native historical links must resolve to their verified prior reports. Expose the history archive only when finalized runs are available, including other modes; a first run must not advertise an empty archive.
- [x] Freeze native history bytes into the current report route. Refresh/late history produces a distinct route when its history changes. Live previews retain their pending-publication/accounting label and use their start timestamp for derived viewer metadata; do not invent a finalized manifest.
- [x] Bound optional prior-history preparation under the selected viewer deadline, reserving time for current evidence. Construct the optional source inside this fallback boundary. If history construction/retrieval/verification fails or its child deadline expires, open the verified current attempt without history and display an explicit history-unavailable notice. Never fall back to S3 in secondary-only mode. Retain and drain timed-out synchronous history work without consuming the current report’s remaining time. Permit at most one unfinished history preparation per viewer; repeated clicks open current evidence without starting more reads until it settles. Cleanup must preserve retry ownership until that work settles; preserve external cancellation.
- [x] Verify passed/failed/live/finalized selection, unrelated tests and modes, parameters, self/future exclusion, bounded history, source isolation, missing/corrupt history, timeouts, cancellation, immutable routes and real Allure CLI/HTTP history links; renew exact coverage, installed-package and public-boundary gates with independent review.
- [ ] Human review the same completed test through the shortcut and history viewer with both history-source policies. No additional paid inference is required for deterministic acceptance.

## Acceptance

- [x] Controlled tests cover live/completed tables, all terminal outcomes, hidden
  multi-selection, keyboard focus, exact deep links, retries, late publication,
  missing/corrupt data, screenshot access, readiness failure and cancellation.
- [x] Prove original DEBUG browsers and accounting/suite exclusion stay intact;
  verify owned server/browser/staging disposal and cleanup retry. Complete exact
  applicable coverage, installed/package/license/boundary and independent review.
- [ ] Human review one completed test while another remains running, then a
  completed suite; inspect its original screenshot and confirm cleanup on exit.
  No paid inference is required for controlled acceptance.

A missing retained snapshot produces an explicit unavailable-evidence message;
no latest-run substitution is allowed. Main-menu history uses on-demand per-run
generation. Application E2E execution remains separately planned; controlled
E2E-mode result tables exercise this shared presentation contract.

Live preview retention has an aggregate 128 MiB byte budget across distinct
snapshots, including shared finalized copies counted once. An over-budget
attempt loses any earlier binding and reports its specific unavailable reason;
it never substitutes another attempt. Finalized history remains independently
viewable. Returning to selection or starting a suite clears retained copies,
budget accounting and unavailable reasons.

- [x] Completed integration/E2E tests open details on Enter for every outcome, including passed tests with no LLM use. Show existing timing, per-test cost and actual token data. `c Close` returns to the same results/row/scroll without confirmation or disposal. Details expose the same exact-attempt `a View in Allure` action, retaining results, snapshots and inspection resources.

## Persistent selected-report preparation

- [x] Show orange `Preparing selected integration run in Allure…` or `Preparing selected E2E run in Allure…` immediately above the footer while a selected-report request is pending. This is persistent screen state, not a toast. Preserve it when remaining tests finish, when results refresh, and when details/confirmation screens resume.
- [x] Clear only the matching request's status after its actual link notification or failure; cancellation and leave/close clear it. Stale requests cannot erase a newer status. Keep errors and current test timing/outcomes independent. Verify partial/final completion ordering, failure, cancellation, resumed views and selected mode.
- [ ] Human review the persistent orange preparation status from a partial completion through the completed-results transition, until the selected report link or failure appears.
