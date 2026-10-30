# Clear configuration visit membership

[Configuration navigation](implementation_plan_a9d.md)

- [x] On entry, list only declared settings actually persisted in the applicable
  `.env` files. Omit unsaved defaults and environment/CLI-only settings.
- [x] Retain that visit's setting names after deletion, with unchecked rows and
  refreshed effective values. Refresh membership on the next entry, rather than
  adding settings that appear while the screen is open.
- [x] Include persisted settings hidden from Review configuration, including
  explicitly empty or false values. Retain deleted hidden rows for the current
  visit; subsequent Review continues to honor their visibility declarations.
- [x] Preserve confirmed deletion from all applicable targets and existing
  automatic Review on closing Clear when visible values remain unacknowledged.
- [x] Derive checkmarks and deletion eligibility from saved assignment presence,
  even when an environment/CLI value overrides it or its saved value is invalid.
  Preserve higher-precedence values after deleting only the saved assignments.
- [x] Verify empty/missing files, retained hidden rows, multiple targets,
  external edits and repeated entry with mounted terminal regressions.
- [x] Complete applicable unit coverage and independent review.
- [x] Human accepts Clear and subsequent Review behavior, including last-deleted-row focus, without another manual retest.

## Human acceptance

Implementation, regression checks, exact unit coverage and independent external
review are complete. Explicit human acceptance is recorded above without another
manual retest; earlier configuration approvals remain recorded separately.
