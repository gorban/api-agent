# Unit coverage progressive disclosure

[TUI requirements](implementation_plan_a.md)

## Requirements

- [x] Preserve pytest progress, tracebacks, final status, and exact per-file line/branch gates. Partial selections retain full measurement scope with informational coverage.
- [x] Present coverage once through a shared console/TUI formatter. When gaps exist, show at most 20 incomplete files, a single-character ellipsis, and complete/incomplete file counts. Fully covered runs show one success line.
- [x] Contribute hidden `UNIT_COVERAGE_FULL=False` through normal configuration precedence. Enabling it shows all files and all missing-line/branch details; it does not change measurement or gates.
- [x] Retain per-run, per-owner JSON, HTML and raw coverage data under the active repository's ignored `artifacts/unit/` directory. Link the surviving JSON and HTML paths after execution. Measure unselected owners too, including namespace packages and active provider production sources, without recording units in Allure.
- [x] Verify truncation, exact thresholds, configuration wiring, missing lines/branches, durable artifacts, owner separation, unavailable reports and full output. Require applicable units, exact line/branch coverage, wheel and public-boundary checks.
- [x] Human review: repeat a partial launcher command, inspect the compact table and open its retained HTML/JSON. Repeat with `--unit-coverage-full` and confirm all files appear once.

JSON retains complete `missing_lines` and `missing_branches` arrays. HTML highlights source lines and branch gaps. Artifacts remain local, are not published automatically and are not pruned by this feature.

## Verification

The public suite passed 5,834 tests and 27 subtests with exact 100% lines and branches across 133 measured files (17,575 statements, 6,450 branches). Independent adversarial checks rejected nine isolated mutations and verified exact near-100 gating, namespaces/active-provider scope, distinct owners/runs and partial-copy failures. Both real launcher variants passed five installed-provider tests; default output contained 20 rows, full output all 133, and both retained usable JSON/HTML. Wheel content, license and public-boundary verification passed. Human acceptance is complete for this coverage-disclosure slice.

The user explicitly replied “Coverage approved” after the guided compact/full console output, retained JSON/HTML and interactive presentation walkthrough. This acceptance covers the coverage-disclosure scope.

Configuration/startup navigation has its own [requirements grouping](implementation_plan_a9d.md).

[Large unit selections](implementation_plan_a9e.md) preserve execution when
parameterized identities exceed the operating system command argument budget.

[Dependency-aware test domains](implementation_plan_a9f.md) are an unimplemented
future proposal, scheduled after all profiling and authentication work.

[Bounded unit execution and launcher parity](implementation_plan_a9h.md) address source aliases, fixture coordination and hang diagnostics without reopening accepted coverage disclosure.
