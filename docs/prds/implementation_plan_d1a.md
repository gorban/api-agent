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
  cleanup when canceling/leaving results, returning to selection, explicitly
  ending inspection or quitting. Preserve pending cleanup and retry duties.
  Browser closure alone must not cancel tests. Never stop shared reporting
  storage or another owner's viewer merely because this viewer closes.
- [x] Reuse only an exact verified viewer binding, or explicit separate run
  routes. Handle foreign/different-run port conflicts clearly; no silent report
  replacement, latest-run substitution or unapproved port switching.

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
