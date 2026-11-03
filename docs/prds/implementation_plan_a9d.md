# Configuration review diagnostics and navigation

[TUI requirements](implementation_plan_a.md) · [Coverage disclosure](implementation_plan_a9.md).

[Multi-test DEBUG confirmation](implementation_plan_a9g.md) protects selected
integration and future E2E execution from unexpectedly opening several browsers.

Configuration review offers reproducible settings without making valid defaults
an operation prerequisite. Preserve startup intent separately from execution
authorization and main-menu navigation.

## Current contract

- [x] Missing active requirements block an operation and identify the latest
  missing setting in red above the shortcuts. Refresh the diagnostic after edits;
  keep Required rows visible even when default acknowledgment is disabled, and
  preserve configuration source precedence.
- [ ] Complete [runtime-discovered configuration requirements](implementation_plan_a9k.md)
  for Run local without executing provider probes during configuration review.
- [x] Offer pending visible values at startup and after Clear .env. Valid effective
  defaults do not block menu or noninteractive operation entry.
- [x] Complete [startup intent and authentication return](implementation_plan_a9a.md):
  initial `c Close` resumes the requested mode without losing selectors; manual
  Review uses `c Close / menu`. Initial auth setup returns through Review
  once; later manual setup closes to the menu.
- [x] Implement [operation-required forward continuation](implementation_plan_a9c.md):
  selecting a mode from the menu binds its required-value Review to that mode.
  Close must revalidate and continue forward after correction.
- [x] Preserve compact-terminal visibility, selection identity and shutdown-safe
  shortcut refresh. Existing START and E2E acceptance continue to govern execution.

## Evidence and acceptance

- [Startup requirements and test mappings](implementation_plan_a9a.md)
- [Earlier requirements, corrections and validation](implementation_plan_a9b.md)

The preceding correction passed all 5,797 applicable units at exact 100% line and
branch coverage. Independent review and targeted mutations close reported issues.
Human acceptance remains recorded in the detail; earlier gates do not establish
acceptance of later navigation changes.

## Deferred human review

Human review may be unrecorded or explicitly deferred. Linked completed scopes
retain their review items; completion does not grant human acceptance.

- [Startup intent and authentication return](implementation_plan_a9a.md)
- [Configuration review history](implementation_plan_a9b.md)

- [Operation-required forward continuation](implementation_plan_a9c.md)

Current human acceptance covers the delivered terminal configuration, test selection/launch controls, results/navigation and timing presentation. Future operation consumers and unrelated technical gates remain separate.

- [x] Correct [Clear configuration visit membership](implementation_plan_a9j.md).

## Controller-sensitive review membership

- [x] Shared configuration-review resolution may span operation choices before
  one is selected, but must honor
  explicit non-operation applicability controllers, including nested alternatives.
  The UI consumes that resolved applicability rather than evaluating backend
  conditions separately. Show Jaeger settings only for OTEL and LangFuse settings only for LangFuse;
  empty trace mode shows neither group. Do not hardcode backend field lists.
- [x] Recompute both displayed rows and pending-default membership after edits,
  including when a saved inactive value exists. Preserve source precedence,
  explicit runtime-required recovery rows and normal operation admission.
- [x] Clear .env continues to expose saved inactive settings for deletion without
  altering or deleting them merely because the selected mode changed.
- [x] Verify actual tracing declarations and fresh/saved mode transitions with
  mounted tests, operation-independent review and generic predicate controls;
  renew coverage, independent/external and public-boundary checks.
- [x] Human accepted the corrected conditional review after pulling the repair.


## Dependent configuration ordering

- [x] Declare TRACE_TARGET applicable only with LangFuse or OTEL selected. Preserve saved inactive targets for later reuse and deletion through Clear .env; do not invent a destination while tracing is disabled.
- [x] Leave ALLURE_S3_USE_LOCALSTACK omitted until explicitly chosen. Require True or False for non-collection integration/E2E reporting, except when reporting is explicitly skipped. Unit coverage does not require S3 or Allure.
- [x] Reveal S3 bucket, endpoint, prefix and region only after that choice. True supplies managed bucket/endpoint defaults and reveals the local port; False requires an external bucket without starting managed S3. Use declaration predicates, not special display conditions. Saved assignments remain removable even while inactive.
- [x] Complete focused rejection checks, applicable coverage, package and independent review gates. All 23,786 unit tests pass with exact 100% line/branch coverage; four causal declaration mutations are rejected. Wheel, source-distribution rebuild without Git, relocated import and mounted Review checks pass.
- [ ] Human acceptance: switch trace modes and local/external storage in Review configuration; verify defaults, required prompts and saved-value cleanup.
