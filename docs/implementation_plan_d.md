# Allure reporting and persistent test results


## Checklist

- [ ] Establish Allure output before the first rewrite integration or E2E test, including the initial "Ok" milestone. Record integration and human-accepted E2E runs through the interactive TUI only, after explicit continuation from the test picker. Neither execution mode supports `NO_STD_IN`; collect-only is not a recorded test run. Unit tests must not be Allure-recorded.
- [ ] Organize Allure results as `<repo> -> <test mode: integration / E2E> -> <test file name> -> <test>`. Map repository, test mode, and test file to the suite hierarchy and retain individual test names. Use the test mode selected through `TUI_MODE`; keep LLM provider authentication configuration separate.
  - [ ] Verify this hierarchy explicitly: the reference artifact-upload commit establishes staging/persistence, work with the user to come up with explicit explicit parent-suite/suite/sub-suite labeling
- [ ] Keep unit tests deterministic and independent of live LLM execution. Require a 100% unit-test pass rate for every commit; this is distinct from the final 100% code-coverage target.
- [ ] Persist all Allure results in S3 from the initial "Ok" milestone onward, retaining successive runs as a history of implementation development rather than overwriting earlier runs. Use LocalStack for local S3 testing.
- [ ] Add optional typed configuration for a secondary Allure results destination, such as a local directory. When configured, write results there as well as to S3; it must not replace primary S3 persistence.
- [ ] Provide the TUI's "Allure serve" mode for viewing test results. Default to the most recent recorded integration/E2E run for the selected repository/pack; unit runs must not replace that selection.
  - [ ] After the report server is ready, automatically open its URL in the host's default browser, including when entering Allure mode through the `a` shortcut. Support host-browser forwarding when the TUI runs inside Distrobox. Also display the URL; if browser opening fails, keep the report available and report the opening failure.
  - [ ] Track completed/finalized run identities in persistent history and resolve the latest run from run metadata, not arbitrary file modification times or stale cached reports. Load the selected run's artifacts locally or from S3 as needed without combining results from unrelated runs.
  - [ ] Support `a` from the completed test-list/summary screen as an immediate transition into Allure mode, pinned to that screen's run ID. A concurrently completed run must not redirect this shortcut away from the results the user was viewing. If no recorded run exists, show that clearly.
  - [ ] Test latest-run selection and the results-to-Allure transition deterministically, including failed/interrupted finalized runs, newer concurrent runs, and a subsequent unit run.

- [ ] Before the first integration/E2E run, establish [token and suite-wide quota tracking](implementation_plan_f.md). Retain usage CSV artifacts and before/after `pctUsed` snapshots and delta with each run, and include the suite delta in TUI results.

- [ ] Require [Playwright UI evidence](implementation_plan_a3.md) for every E2E run: original `response_text` and `response_html` when present, plus rendered screenshots after maximizing the agent modal and scrolling to the top. Retain both outputs and complete scroll-region evidence where needed, including debug and confirmation attempts, under the correct test/run identity in S3 history.
