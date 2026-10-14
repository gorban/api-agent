# Startup intent and configuration review

[Configuration review](implementation_plan_a9d.md)

## Requirements

- [x] Track startup navigation independently of automatic execution. Explicit
  startup mode Review uses `c Close`, revalidates active requirements, and resumes
  the requested mode. Missing requirements keep Review with its current red
  diagnostic; valid unsaved defaults allow continuation.
- [x] Preserve positional selectors until the first unit/integration/E2E picker.
  Opening a picker does not execute tests. A deliberate return from that picker
  to the menu clears startup selectors; existing START and E2E acceptance rules
  still control execution.
- [x] Main-menu operation selection uses valid effective values without default
  review. Actual missing active requirements still open Review. Manual Review and Review after Clear .env use `c Close / menu`.
  Operation-required Review follows [forward continuation](implementation_plan_a9c.md).
- [x] Startup authentication Skip, remembered Skip, and Setup/Close continue
  through pending-value Review once, then the menu. Explicit unattended-auth
  startup opens the wizard first, then follows that same Review/menu path without
  reopening the wizard. Startup wizard uses `c Close`; a later menu wizard uses
  `c Close / menu`. Supply navigation context explicitly rather than infer it
  from the selected operation.
- [x] With no visible pending values, continue directly to the intended
  destination. Preserve hidden-required guidance, declaration visibility,
  collection/help shortcuts, configuration precedence and secret protection.
- [x] NO_STD_IN missing active requirements produce actionable stderr messages
  and exit 2 without a TUI, stdin reads or prerequisites. Unsaved defaults do not
  block noninteractive operation. Existing mode/START restrictions remain.

## Acceptance

- [x] Mounted parameterized unit/integration/E2E tests cover startup selectors
  with defaults and missing requirements, blocked collection, correction,
  continuation, shortcut labels, no unintended execution and later menu reset.
- [x] Mounted authentication tests cover initial Skip/remembered Skip, initial
  Setup/Close, explicit auth startup and later manual wizard entry. Assert the
  Review visit and labels, including empty-review and provider refresh paths.
- [x] Noninteractive tests prove required rejection and default-only continuation
  without secondary failures masking the intended guard.
- [x] Independent adversarial review and focused mutation checks close findings.
- [x] Applicable units pass exact 100% line/branch coverage before code commit.
- [ ] Human acceptance of standalone startup, picker and authentication navigation.

## History

Human review of `52a40a2` identified lost startup mode/selectors after Review.
That correction changed the explicit-mode test to accept a menu detour; the
unchanged selector test contained no pending review values. The approved repair
covers their combination rather than treating coverage totals as acceptance.

## Requirement-to-test mapping

- [Startup/navigation checks](../../tests/unit/test_tui_review_navigation.py): all three modes, selector lifetime, required guards, exact labels, wizard return, remembered Skip/restart, cleanup context and auth collection shortcut.
- [Noninteractive checks](../../tests/unit/test_review_noninteractive.py): missing requirements and unsaved defaults across unit/integration/E2E, ordered selectors and no input/prerequisites before rejection.
- [START checks](../../tests/unit/test_start_tui.py), [authentication wiring](../../tests/unit/test_auth_wiring.py), [visibility checks](../../tests/unit/test_configuration_review_policy.py) and [provider refresh](../../tests/unit/test_provider_selection_refresh.py): accepted execution, authentication context and declaration replacement.
- [Existing TUI compatibility](../../tests/unit/test_tui.py), [clipboard](../../tests/unit/test_clipboard.py) and [preflight privacy](../../tests/unit/test_operation_preflight.py): direct startup continuation without redundant manual mode selection.

Independent review passes 143 focused checks and 10 final regression checks. Seven
focused navigation mutations reject the intended regressions; six noninteractive
missing-requirement cases reject removal of their guard. The complete unit and
exact-coverage gate passes before committing. Human acceptance remains open.

The first complete run passed 5,788 units but identified an uncovered manual
setup fallback. Explicit manual context now always returns to the menu, including
stale START intent; startup context retains its authorized continuation. Independent
review and a focused mutation verify this correction. Final exact coverage follows.

Final validation: 5,789 applicable units pass; all 17,802 statements and 6,692
branches are covered. The manual-context correction also passes 122 focused
checks, renewed independent review and all 20 START compatibility checks. Public
index/metadata audit and scoped delivery follow; human acceptance remains open.
