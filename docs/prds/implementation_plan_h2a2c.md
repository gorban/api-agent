# Secondary Allure history restoration

[Reporting lifecycle requirements](implementation_plan_h2a2.md)

- [x] Offer an explicit action from configured `ALLURE_SECONDARY_DIR` to configured `ALLURE_S3_BUCKET` and prefix. Refuse absent source or destination and never infer a restore from a preview, collection, or ordinary history browsing.
- [x] Bound the scan, manifest count and size, artifact count and individual/aggregate bytes, and total operation time. Validate original run UUIDs, manifest identities, result identities and attachment references. Reject symlinks, nonregular files and content changes; never read a file again after verifying different bytes for publication.
- [x] Verify a complete local run before sending any of its objects. Refuse a different destination manifest observed at preflight before adding artifact keys. Use conditional writes: equal bytes are idempotent, differing bytes fail without overwrite. Publish the original manifest last as the sole visibility point; cancellation, failure or a lost concurrent publication may leave unreferenced artifacts for exact-byte retry. Prove the competing-publisher race with controlled storage and a manifest-based reader.
- [x] Reuse the workbench reporting session for managed storage ownership and keep the explicit restore task responsive to cancellation and Close/Quit. Report bounded progress and safe errors without credentials or artifact contents. Refresh the history viewer after successful restoration; retain the secondary files and all existing destination evidence.
- [x] Prove missing, equal, conflicting, partial, corrupt, symlinked, oversized and late-finalized runs with controlled S3 and local IPC fixtures. Check cancellation and rerun behavior. Do not use real history or live bucket writes for automated proof. Independent adversarial controls include SDK-error disclosure, cancellation at storage boundaries, and future-artifact rereads.


- [x] Complete the joined unit coverage, installed-package, license and public-boundary gates before delivery.
- [ ] Human acceptance: review the explicit action and progress against disposable history; verify equal reruns, conflicting destinations and cancellation preserve original evidence.

Restoration holds the suite publication lock, freezes verified bytes per run,
publishes the original manifest last, and reuses the managed reporting session.
A failed later run does not roll back earlier finalized runs. Cancellation waits
for an already-started bounded storage call to settle before releasing ownership.
Unexpected ordinary storage, progress, reporting setup, or viewer errors display only controlled messages; cancellation and other interruptions still propagate.

Technical implementation and independent/external review are complete under the concurrent-publication contract below. The final joined 32-worker gate passed 10,955 tests and 27 subtests at exact measured line and branch coverage; installed-package, mutation, license and public-boundary checks passed. Human acceptance remains open.

## Controlled callback diagnostics

- [x] Typed callback errors are not trusted by class alone: only exact public messages from a finite set may reach the restore UI. Ordinary history opening, selected-report preview, and post-restore viewer refresh use the same bounded-display policy, retaining known CLI setup and occupied-port guidance through exact messages or validated numeric templates. Cancellation and other interruptions still propagate.

## Concurrent destination publication

An independently committed manifest may appear after the preflight read and before the restore's final conditional manifest write. In that race, the restore must fail without overwriting the competitor's manifest or any immutable object. Artifact keys uploaded by the losing restore may remain unreferenced. Readers expose only artifacts referenced by the committed manifest, never an apparent merged run. No automatic orphan cleanup occurs; a later explicit restore checks exact bytes again. The publication lock coordinates this workbench's suites, not independent destination writers.

## Restore action feedback and admission

- [x] Keep restore preparation and finalized-run progress visible in an orange status line above the history page shortcut bar, through the full owned operation; clear the line after completion or interruption.
- [x] Admit one restore worker across rapid repeated action submissions, including the interval before the worker starts. Repeated submissions must neither replace nor cancel the admitted restore. Permit a new explicit restore after the worker finishes.
- [x] Clear a previous restore error when a new configured attempt starts. Display a bounded failure for the current attempt, and clear that failure on a successful explicit retry.
- [x] Preserve generation checks, threaded cancellation acknowledgement and viewer disposal ordering when leaving through Close or Quit. Controlled native workbench tests cover these paths without real history or storage writes.
- [ ] Human acceptance: inspect the orange history-page progress line and repeated-click behavior with disposable history.
