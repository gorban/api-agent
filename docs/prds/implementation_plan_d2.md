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
- [x] Renew all applicable unit checks and exact measured line/branch coverage.
  Preserve failed gate evidence and diagnose it before accepting a renewed run.

These are test-fixture repairs. Runtime history windows, request deadlines,
cleanup ownership and user configuration retain their existing behavior.
