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

[Bounded unit execution and launcher parity](implementation_plan_a9h.md) is implemented and independently reviewed, covering source aliases, fixture coordination and fixed 60-second hang guards. The user accepts the repaired full launch and integration with and without DEBUG and NO_STD_IN; accepted coverage disclosure stays complete.

[Unit-suite performance investigation](implementation_plan_a9i.md) joins later profiling, preserving exact coverage and real boundary proofs.

## Child interpreter coverage

- [x] Owners may opt into child Python interpreter coverage with standard `pyproject.toml` `[tool.coverage.run] patch=["subprocess"]`. The ordinary unit runner defaults this off, reads only the current owner’s admitted declaration, merges opted-in child execution into that owner’s report, and preserves the same exact line/branch gate in serial and parallel runs. Inherited process coverage configuration is cleared before unit collection and each pytest owner. A captured exact owner project takes precedence even without an explicit patch; a missing owner project abstains rather than hiding mapped pack policies; otherwise same-owner pack declarations abstain when patch is absent, coalesce identical explicit policies, and reject conflicting explicit policies. Require pytest-cov 7 or newer for supported subprocess patching. Opted-in child-only execution must produce measured owner coverage; without opt-in, unexecuted parent production remains a coverage failure.
- [x] Complete independent/external source review, wheel and public-boundary checks; all 27,528 units pass with exact 100% statement and branch coverage.
- [ ] Human launcher acceptance of this extension remains open. Existing coverage disclosure acceptance is unchanged.
