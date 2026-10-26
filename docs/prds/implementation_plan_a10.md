# Failure details and result-list return

[Shared TUI workbench](implementation_plan_a.md)

## Requirements

- [x] Display a failed/error result's reason immediately after its test identity,
  before execution/cost/token metrics. Preserve accounting and actual pytest
  tracebacks. If no reason exists, state that no diagnostic was captured instead
  of displaying only ERROR.
- [x] Integration failures before normal per-test reports retain a meaningful
  stage, allowlisted error category and bounded public-code stack locations.
  Keep arbitrary exception bodies, external paths and command output out of
  safe runner events, telemetry and exported reports. Preserve normal pytest
  evidence contracts. Local interactive failure details have a separate transient
  diagnostic channel as specified below; never capture frame locals.
- [x] Missing or truncated worker reports retain exit evidence through TestResult,
  runner events, TUI and Allure. Complete setup failures/skips still finish
  normally; incomplete reports finalize after worker exit, with exactly one
  per-test completion callback. Diagnostic work does not change execution outcomes.
- [x] Returning from details selects the viewed test by identity in the shared
  unit/integration/E2E results list, with table focus and scroll restored. Repeated
  refreshes and row reorder preserve that identity. A missing identity or a
  replacement run falls back safely to the first visible row.
- [x] Preserve stable failure-first ordering and first-completion selection of the
  first failure. Later refresh or details close must not reset to the top row.
- [x] Keep the currently failing integration execution unchanged for human review.
  Do not repair prerequisites, provider setup, configuration/authentication or
  test execution until the user verifies failure details on two failed tests.

## Acceptance and test mappings

- [x] [Diagnostic proofs](../../tests/unit/test_integration_failure_details.py)
  cover pre-run, collection, backend/process startup and missing/truncated reports;
  reasons survive events and Allure, secrets stay absent, and causes precede metrics.
- [x] [Selection proofs](../../tests/unit/test_failure_return_selection.py)
  parameterize unit/integration/E2E with two failing tests: open the second, Close,
  preserve the second, then open/return to the first. Cover long-list scroll/focus,
  refresh/reorder, removal during details and replacement-run fallback.
- [x] Existing [accounting display](../../tests/unit/test_result_accounting_display.py),
  [TUI setup](../../tests/unit/test_integration_tui_setup.py), shared presentation
  and safe setup-failure checks remain green without live provider calls.
- [x] Independent adversarial review, targeted mutations, exact complete unit
  line/branch coverage and public index/metadata checks pass before committing.
- [x] Human confirms selection is retained when returning from failure details.
- [ ] Human verifies useful, complete available causes on two failed tests.
- [ ] Only after that verification, investigate and repair integration execution
  in a subsequent scoped batch, retaining this presentation behavior.

## Human review sequence

Restart the launcher and run two selected integration tests. Open the second
failure, read its diagnostic, and Close back to the same selected row. Open the
other failure and repeat. Execution repair remains gated on explicit verification;
continued testing or elapsed time does not close the gate.

The initial diagnostic-first sequence above is retained as history. Subsequent explicit requests authorized [calibration maintenance](implementation_plan_b2b1.md) and [bounded admission recovery](implementation_plan_f2c.md). Those narrow authorizations do not establish broad human diagnostic acceptance or close the separate live cache finding.

## Evidence

All 5,811 applicable units pass; exact 17,862 statements and 6,720 branches
are covered with zero missing lines/branches. Sol low implementation and Sol high
adversarial review pass, including five focused mutations proving diagnostic
retention, privacy, selection return, incomplete-report finalization and protocol
deduplication. The intended public index and metadata pass their boundary checks.

The first complete run found one obsolete withheld-message assertion and one
uncovered deduplication branch. Stronger test assertions and a repeated malformed
event case resolved both; production execution behavior stayed unchanged.
A controlled synthetic two-failure fixture retains distinct assertion/exception
causes; it is test evidence, not a deliberately failing permanent integration suite.

## Local diagnostic completeness follow-through

- [x] Local failure details display the actual exception type/message, traceback
  and explicit/implicit exception chains, including visibly labeled suppressed
  contexts, without frame locals. Append captured
  command evidence from each visible chained exception. A stage/category alone
  is insufficient when the local process has more diagnostic information.
- [x] Keep richer diagnostics transient in a separate per-test local field.
  Do not place them in safe result tracebacks, runner events, telemetry, Allure,
  artifact attachments or generic result representations. Existing pytest
  tracebacks retain their evidence policy.
- [x] Prerequisite command failures retain the command, exit status, and bounded
  stdout/stderr tails, including timeout/cancellation/cleanup failure evidence.
  Preserve the existing combined output bound, owned cleanup and nonzero result.
  Indicate truncation explicitly rather than presenting a tail as complete output.
- [x] Swallowed runner callback, output-reader and owned cleanup errors retain
  their local causes, including after a per-test result has finalized. Enrich
  local final outcomes without changing test statuses, execution, safe events or
  callback counts. Preserve actual pytest tracebacks beside supplemental causes.
- [x] Incomplete pytest reports include available worker-output evidence locally
  alongside exit status. Do not repair the process failure or retry the test.
- [x] [Local cause and reporting proofs](../../tests/unit/test_integration_failure_details.py)
  cover explicit/implicit/suppressed chains, callback/cleanup errors, incomplete
  workers and recorded-wrapper isolation.
- [x] [Command evidence proofs](../../tests/unit/test_prerequisite_local_diagnostics.py)
  exercise real synthetic processes, both output streams, truncation, interruption,
  post-cleanup status and cleanup chains. Assertions inspect captured streams.
- [x] Passed rows with a local run-level cause can open details even without
  token usage/history, while keeping their individual status.
  [Mounted unit/integration/E2E checks](../../tests/unit/test_failure_return_selection.py)
  verify real Enter/Close access and selection return.
- [x] [Late reader attribution](../../tests/unit/test_debug_finalization_coverage.py)
  preserves late prior-suite errors without attaching them to the next suite.
- [x] Independent adversarial review/mutations and the complete applicable unit
  checks pass for the follow-through before its scoped commit, with exact coverage.
- [ ] Human reviews diagnostic completeness before integration execution repairs.

The first presentation checkpoint used fixed classifications to protect shared
reporting. Human review confirmed navigation but exposed insufficient local
causes: prerequisite errors were classified as unknown and stderr was discarded.
This follow-through separates useful local diagnostics from exported evidence.

Follow-through evidence: all 5,837 cases in the complete unit run pass. Two added
prelaunch interruption cases also pass on unchanged production code; appending
their coverage completes exact 17,950 statements and 6,744 branches with zero
missing lines/branches. Sol high review closes chain, stream-assertion, late-reader
and interactive-access findings. Focused mutations reject lost local display,
stderr capture, recording isolation, reader attribution and details access. The
429-file public index/metadata audit passes with no new exceptions.

A preliminary full run was intentionally stopped after review found the passed-row
access gap; it is not a passing-gate claim. No live integrations, configuration
changes or underlying execution repairs are included. Human diagnostic acceptance
remains open; prior selection return is already accepted.

## Noninteractive diagnostic follow-through

- [x] Print full captured failures to NO_STD_IN CI stderr with credential-only
  scrubbing, independently of inference content logging. Preserve cleanup/exit
  contracts and safe report boundaries. See [CI requirements and acceptance](implementation_plan_a10a.md).
- [ ] Human reviews CI output before execution fixes. The local port-conflict
  cause is now visible; that does not approve automatic resource reuse or cleanup.

## Details before suite completion

- [x] [Open completed tests while remaining tests run](implementation_plan_a10c.md).
