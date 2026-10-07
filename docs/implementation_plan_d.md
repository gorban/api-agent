# Allure reporting and persistent test results


## Checklist

- [ ] Establish Allure output before the first rewrite integration or E2E test, including the initial "Ok" milestone. Record integration runs from the TUI and supported unattended CLI invocations, and human-accepted E2E runs through the TUI. Unit tests must not be Allure-recorded.
- [ ] Organize Allure results as `<repo> -> <test mode: integration / E2E> -> <test file name> -> <test>`. Map repository, test mode, and test file to the suite hierarchy and retain individual test names. Use the test mode selected through `TUI_MODE`; keep LLM provider authentication configuration separate.
  - [ ] Verify this hierarchy explicitly: the reference artifact-upload commit establishes staging/persistence, work with the user to come up with explicit explicit parent-suite/suite/sub-suite labeling
- [ ] Keep unit tests deterministic and independent of live LLM execution. Require a 100% unit-test pass rate for every commit; this is distinct from the final 100% code-coverage target.
- [ ] Persist all Allure results in S3 from the initial "Ok" milestone onward, retaining successive runs as a history of implementation development rather than overwriting earlier runs. Use LocalStack for local S3 testing.
- [ ] Add optional typed configuration for a secondary Allure results destination, such as a local directory. When configured, write results there as well as to S3; it must not replace primary S3 persistence.
- [ ] Provide the TUI's "Allure serve" mode for viewing test results.

- [ ] Before the first integration/E2E run, establish [token and suite-wide quota tracking](implementation_plan_f.md). Retain usage CSV artifacts and before/after `pctUsed` snapshots and delta with each run, and include the suite delta in TUI results.
