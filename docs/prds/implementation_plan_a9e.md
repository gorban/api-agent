# Large unit selections

[Unit execution and coverage](implementation_plan_a9.md)

- [x] Preserve validated, ordered per-owner test identities in an operation-owned
  temporary file instead of exceeding OS command argument limits. Read the
  selection before pytest executes; remove it with the existing temporary
  workspace. Keep coverage, event capture and selector semantics unchanged.
- [x] Verify a selection larger than the command argument budget, exact identity
  transfer and temporary-file retirement, plus actual pytest execution and a
  mutation restoring argument expansion.

Full selection behavior remains covered by the existing actual-process runner
checks; the budget regression verifies exact transfer and rejects restored
argument expansion. This transport repair does not change coverage or test
selection policy.

- [ ] Traverse the selected owner's common directory once, admitting only selected
  files and their ancestor directories. Preserve explicit-file collection behavior,
  owner conftest fixtures and lexical symlink aliases; do not import unselected
  files. Collect each selected unit file once, then retain only the exact admitted
  parametrized node identities in their requested deduplicated order. Reject
  missing identities before executing any test; execute no unselected cases.
  Apply the same deterministic selection in serial and work-stealing workers.
  Preserve operation-owned transport, full production coverage scope, event
  reporting and fixed 60-second guards. Verify real-process subset, ordering,
  duplicate, stale-identity and parallel-worker behavior before acceptance.
