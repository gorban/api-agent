# Accounting cache connection lifecycle

[Accounting requirements](implementation_plan_f3.md).

- [x] Trace delayed SQLite ResourceWarnings to actual allocations, distinguishing fixture verification connections from production cache ownership. Do not suppress diagnostics or infer disposal from coverage alone.
- [x] Explicitly close raw test readback connections after their transaction context exits. Verify row preservation and rollback independently of the closed-connection assertion.
- [x] Prove immediate, idempotent HistoryCache close and exactly-once garbage-collection fallback. Failed and interrupted initialization retain rollback and close their original connection.
- [x] Independently review assertions with isolated mutations removing each intended close. No production repair is needed when the warnings originate solely in fixtures.
- [x] Renew the full unit/exact coverage, wheel and boundary checks.
- [ ] Human review of the fixture-only diagnosis and disposal evidence.

Allocation tracing plus forced garbage collection reproduced two warnings in raw migration-test readbacks; the same traced run emits zero after using explicit closing contexts. Production HistoryCache already owns its connection through a finalizer and disposes failed initialization. Two straight-line lifecycle tests also verify immediate close before collection. Independent review passes 88 focused tests and detects six close-omission mutations. Existing data/estimation behavior is unchanged, and user history is not a test input.

Full verification passes 5,813 public units plus 27 subtests, with exact 100% line/branch coverage in 132 measured files. No SQLite ResourceWarning remains; three unrelated fork diagnostics remain. The wheel, whitespace and 392-file indexed boundary audit pass. Required licenses and real history/settings are unchanged.
