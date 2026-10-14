# Operation-required configuration review

[Configuration review navigation](implementation_plan_a9d.md)

## Requirements

- [x] When an operation's editable active requirements are missing, bind Review's
  return destination to that operation regardless of whether it was selected at
  startup or from the main menu. The shortcut and compact instructions use
  `c Close`, without `/ menu`.
- [x] Close revalidates current effective values. Missing requirements retain
  Review and refresh the latest red requirement diagnostic above the shortcuts;
  do not collect or execute the operation. Once all active requirements are valid,
  Close proceeds directly to the selected operation without another menu selection.
  Other unsaved defaults remain nonblocking.
- [x] No user-visible menu/back action bypasses operation Review. Input cancellation
  returns to the same Review; quitting remains available. After reaching a picker,
  its normal Close/menu behavior remains available.
- [x] Manual Review and Review injected after Clear .env remain independent of an
  operation and use `c Close / menu`. Returning deliberately from a picker clears
  prior startup selectors; later menu navigation does not revive that startup.
- [x] Preserve explicit-startup selectors, initial/manual authentication context,
  START execution authorization, E2E acceptance, hidden-required guidance,
  configuration precedence, compact display and noninteractive error behavior.

## Acceptance and test mapping

- [x] [Navigation checks](../../tests/unit/test_tui_review_navigation.py) parameterize
  unit/integration/E2E and compact/full terminals: menu → missing requirement →
  repeated Close remains Review → actual UI save → Close reaches picker. Assert
  exact label, red diagnostic, no early collection/execution and nonblocking defaults.
- [x] Navigation checks reproduce explicit startup → filtered picker → menu →
  Clear .env deletion → independent Review/menu → operation-required Review →
  correction → picker. The final picker has ordinary menu-selected scope.
- [x] Existing [startup/auth checks](../../tests/unit/test_start_tui.py) and
  [noninteractive checks](../../tests/unit/test_review_noninteractive.py) remain green.
- [x] Independent review, targeted destination/guard/label mutations, exact complete
  line/branch coverage and public-boundary audit pass before a scoped code commit.
- [ ] Human review confirms forward continuation from a menu-selected operation.

## Requirements history

The earlier approved startup repair retained menu-operation Review's menu return.
Human follow-up changes that decision: any operation-required Review must resume
its selected operation after correction. The distinct manual and Clear .env paths
continue to return to the menu.

Human testing of `8cb5cef` confirmed the required-field diagnostic for startup and
menu entry, noninteractive error/nonzero exit, and explicit startup continuation
or revalidation on Close. The remaining finding concerns menu-operation return.

Sol low implementation and Sol high adversarial review close the behavior
change. The 34-case navigation suite passes; revised six repair cases pass with
invalid and valid integer submissions. Noninteractive/START compatibility passes
94 checks. Independent review passes 15 mounted probes and rejects all six
targeted destination, guard, label and post-repair forwarding mutations. The
complete exact-coverage gate passes; human acceptance remains open.

Final validation: all 5,797 applicable units pass; all 17,803 statements and
6,692 branches are covered. Interactive compatibility passes 126 tests. The
public-boundary index and intended metadata audit pass with no new line exceptions;
scoped delivery follows. Human testing remains open.
