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
- [ ] Human acceptance of the corrected conditional review.
