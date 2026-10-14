# Configuration review history

[Current navigation contract](implementation_plan_a9d.md)

Historical checkpoints and requirement evolution are retained below. Current
startup navigation and acceptance are tracked in [A9a](implementation_plan_a9a.md).

## Requirements

- [x] Preserve main-menu/manual `c Close / menu`: Review returns to the main menu, including when entered from Clear .env configuration. Selecting an operation again validates its effective values and active requirements.
- [x] When an operation is blocked by configuration, show the most recent missing setting in red directly above the shortcut bar, with the active requirement context such as `TUI_MODE=integration`. Keep that diagnostic outside the scrolling settings area.
- [x] Inject Review at interactive startup and when leaving Clear .env if any visible required/default value is not explicitly supplied. Preserve declaration visibility exceptions and noninteractive/help/collection shortcuts. Defaults remain available to save for reproducibility.
- [x] Menu navigation into an operation blocks only on invalid or missing active required values. Valid effective defaults, including conditional defaults, permit entry without persistence or acknowledgement. Show red diagnostics only for actual missing requirements.
- [x] Saving through Review, loading `.env`, environment variables and CLI options must resolve the same values and conditional defaults when not superseded by a higher-precedence source. Preserve existing precedence; an explicit startup mode must still validate its active requirements.
- [x] Refresh or clear the diagnostic after saving, accepting a default, unsetting, changing the save target or replacing provider declarations. Do not retain a resolved setting as the blocker or display secret values.
- [x] Keep settings selectable in compact terminals and after resizing. Preserve selected-row identity, hidden-required diagnostics, authentication setup and the original explicitly accepted Start/E2E behavior.
- [x] Prevent shortcut-bar refresh before initial mounting completes or after application shutdown begins. Recheck ownership after awaiting child removal. Preserve framework-owned serialized recomposition, current bindings, system children and cancellation/detachment behavior.

## Acceptance and evidence

- [x] Mounted checks reproduce cleanup → review → Close / menu without returning to cleanup.
- [x] Mounted checks verify main-menu/manual review never resumes integration, and operation redirects show an actionable red footer-adjacent blocker.
- [x] Real declaration checks compare LocalStack selection and derived S3 defaults across UI persistence, `.env`, environment and CLI sources.
- [x] Mounted checks verify startup/cleanup review, default-only menu entry, and clearing the diagnostic as soon as actual required values are satisfied, even while unsaved defaults remain.
- [x] Diagnostics advance or clear after edits; compact settings and error context remain visible after resizing.
- [x] Focused mutations prove the intended navigation and diagnostic checks; applicable units and exact line/branch coverage pass before a code commit.
- [x] Independent adversarial review closes all actionable findings.
- [ ] Human review confirms the repaired standalone launcher.

## Requirements history

Human review rejected the selected-operation continuation introduced in `a1daf92`.
The first correction restored ordinary menu navigation and added the missing
configuration diagnostic. Compact visibility improvements are retained. The prior
5,751-test coverage gate was implementation evidence; human acceptance remained open.

The subsequent clarification limits automatic default review to startup and leaving
Clear .env. Effective defaults are valid for operation entry without saving. The
5,767-test gate below predates this clarified policy and does not establish its
acceptance.

## Requirement-to-test mapping

- [Standalone review regressions](../../tests/unit/test_tui_review_navigation.py): ordinary menu return, cleanup return, active-required blockers and nonblocking defaults, compact display, resizing and provider refresh.
- [Startup checks](../../tests/unit/test_start_tui.py): original accepted Start and E2E behavior.
- [Configuration visibility checks](../../tests/unit/test_configuration_review_policy.py): hidden-required diagnostics and visibility contracts.
- [Storage source-parity checks](../../tests/unit/test_localstack_config_source_parity.py): equivalent conditional defaults, actual UI-save operation entry before derived-default persistence, optional acknowledgement, restart and inactive requirements.
- [Compatibility checks](../../tests/unit/test_tui.py), [clipboard checks](../../tests/unit/test_clipboard.py), [provider refresh](../../tests/unit/test_provider_selection_refresh.py) and [preflight privacy](../../tests/unit/test_operation_preflight.py): enter each tested operation after startup Review while preserving its existing assertions.
- [Footer lifecycle checks](../../tests/unit/test_footer_lifecycle.py): real initial mount, concurrent refresh, cancellation, detachment, shutdown before/during refresh and shutdown while waiting for the framework lock. Removing the mount or shutdown guards fails their targeted regressions.

Previous correction validation: 5,767 applicable units pass with exact 100% line and branch
coverage. Independent frozen review passes 55 focused tests. Navigation, diagnostic,
mount/shutdown and system-child mutations reject the intended regressions. Human
acceptance remains open.

Clarified-policy validation: 5,767 applicable units pass; all 17,782 statements
and 6,690 branches are covered. Independent review closes all findings; focused
mutations reject default gates, startup/cleanup bypass, missing-required bypass,
stale default diagnostics and preflight secret exposure. Human acceptance remains
open.
