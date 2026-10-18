# Deterministic unit qualification fixtures

[Test reporting](implementation_plan_d.md)

- [x] Give historical synchronization fixtures an explicit estimator clock that
  includes their immutable synthetic manifests. Synchronization must retain that
  clock, so a calendar boundary cannot silently age a fixture out of its window.
  Keep separate tests of the real rolling-window behavior.
- [x] For transport interruption tests, wait for the actual request to enter its
  blocked read before injecting interruption. Use a generous request safety
  envelope and a separate short cleanup budget for pending-disposal behavior;
  do not race a scheduling sleep against the request's deadline.
- [x] Separate operation cleanup completion from deliberate timeout retention in
  cancellation fixtures. Gate listener/runtime disposal and acknowledge lease
  release before successful-completion assertions; retain the ordinary generous
  completion envelope. A short cleanup budget applies only to a deliberately
  blocked stage, proving the exact retained receipt before bounded retry. Keep
  original body identity, dependency order, exactly-once release and leak-free
  teardown checks. Hold successful downstream disposal beyond the former short
  window after acknowledged entry, so restoring that timing assumption fails
  deterministically. Delayed downstream disposal must not be mistaken for lost
  ownership merely because the listener was unblocked.
- [x] Renew all applicable unit checks and exact measured line/branch coverage.
  Preserve failed gate evidence and diagnose it before accepting a renewed run.

- [ ] Independently review this cancellation-fixture repair and renew its joined
  unit/package gates. The earlier delivered checks above do not qualify this
  changed fixture; preserve human review independently where applicable.

These are test-fixture repairs. Runtime history windows, request deadlines,
cleanup ownership and user configuration retain their existing behavior.
