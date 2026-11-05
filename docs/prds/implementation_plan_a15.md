# Owner unit defaults

## Requirements and acceptance

- [x] Discover every composed unit suite; preserve dependency/base visibility in the picker.
- [x] Default unit selection to the active owner; base launches retain all base units.
- [x] `ALL_UNIT` defaults False. `--all-unit` restores all composed unit selection and production coverage. Expose the field for unit execution only, alongside coverage controls.
- [x] Explicit positional selectors override default selection, including dependency/base identities. They do not change the chosen coverage scope.
- [x] Measure only active-owner production by default. The reusable runner retains composed coverage unless its new `coverage_owner_only` keyword is explicitly enabled.
- [x] Complete owner selection enforces exact 100% line/branch coverage even while the catalog's All tests group is unchecked. Partial selections remain informational.
- [x] Recollection includes new owned units under an intact owner default scope. Explicit exclusions stop expansion of that scope.
- [x] Editing ALL_UNIT reapplies defaults unless explicit startup selectors were supplied.
- [x] An owner with no units does not silently run dependencies; execution explains `--all-unit`. Data-only collection remains full discovery and succeeds.
- [x] Integration/E2E behavior remains unchanged.
- [x] Human acceptance of picker defaults and owner/all execution: owner-local selection and exact coverage, with `--all-unit` restoring composed selection.

## Verification

Synthetic unit tests cover owner/base/all defaults, coverage completeness, repository-scope recollection, explicit overrides and owner-only runner coverage with both selected and unselected dependencies. Existing selection and TUI full-suite confirmation regressions remain applicable.
