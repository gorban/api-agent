# Concurrent cleanup acknowledgement

[Local startup and shutdown](implementation_plan_k2b.md)

Concurrent Stop and startup-settlement owners may observe the same retained cleanup task. A delayed cleanup-pending receipt must not turn acknowledged successful disposal into a failed Stop after another waiter completes that task.

- [x] Decide a delayed receipt from its owning close task and acknowledged disposal, rather than only a mutable pending marker. Preserve real disposal failure, foreign receipts and cancellation semantics; do not acknowledge unfinished work or invoke disposal twice.
- [x] Reproduce the interleaving deterministically with synchronization events, prove failure before repair, and verify successful Stop, cleared pending state and exactly-once reverse disposal afterward.
- [x] Keep negative controls for genuine pending/failed cleanup and complete applicable coverage, independent review and external review.
- [ ] Human confirmation of Stop during startup. Earlier ordinary cleanup acceptance remains valid.

The synchronized regression reproduces the original failed Stop after disposal succeeds. The repair preserves the exact task and locally produced receipt, consumes the task result and retains cancellation. Independent checks cover every new line/branch, real screen behavior, six rejection controls and four causally failing private mutations. Full applicable units pass with exact line/branch coverage; package and external source review close.

**Deferred human review**

Implementation and required automated/independent/external source checks are complete. Only the focused human confirmation remains unchecked.
