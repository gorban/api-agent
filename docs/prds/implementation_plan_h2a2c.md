# Secondary Allure history restoration

[Reporting lifecycle requirements](implementation_plan_h2a2.md)

- [x] Offer an explicit action from configured `ALLURE_SECONDARY_DIR` to configured `ALLURE_S3_BUCKET` and prefix. Refuse absent source or destination and never infer a restore from a preview, collection, or ordinary history browsing.
- [x] Bound the scan, manifest count and size, artifact count and individual/aggregate bytes, and total operation time. Validate original run UUIDs, manifest identities, result identities and attachment references. Reject symlinks, nonregular files and content changes; never read a file again after verifying different bytes for publication.
- [ ] Verify a complete local run before sending any of its objects. Refuse an existing different destination manifest before adding artifact keys. Use conditional writes: equal bytes are idempotent, differing bytes fail without overwrite. Publish the original manifest last; cancellation or failure may leave uncommitted artifacts for exact-byte retry. The committed-destination race guarantee remains under review.
- [x] Reuse the workbench reporting session for managed storage ownership and keep the explicit restore task responsive to cancellation and Close/Quit. Report bounded progress and safe errors without credentials or artifact contents. Refresh the history viewer after successful restoration; retain the secondary files and all existing destination evidence.
- [x] Prove missing, equal, conflicting, partial, corrupt, symlinked, oversized and late-finalized runs with controlled S3 and local IPC fixtures. Check cancellation and rerun behavior. Do not use real history or live bucket writes for automated proof. Independent adversarial controls include SDK-error disclosure, cancellation at storage boundaries, and future-artifact rereads.


- [x] Complete the joined unit coverage, installed-package, license and public-boundary gates before delivery.
- [ ] Human acceptance: review the explicit action and progress against disposable history; verify equal reruns, conflicting destinations and cancellation preserve original evidence.

Restoration holds the suite publication lock, freezes verified bytes per run,
publishes the original manifest last, and reuses the managed reporting session.
A failed later run does not roll back earlier finalized runs. Cancellation waits
for an already-started bounded storage call to settle before releasing ownership.
Unexpected ordinary storage, progress, reporting setup, or viewer errors display only controlled messages; cancellation and other interruptions still propagate.

The joined unit gate passed 10,884 cases and 27 subtests with exact measured line and branch coverage. Installed-package resource checks, meaningful mutations, independent review, licenses and public-boundary checks passed.
External review reopened the destination race policy and unexpected-error boundary; prior gate evidence is historical until the repairs and review complete.
