# Thread-safe process fixture startup

[Unit execution](implementation_plan_a9h.md)

- [x] Replace explicit fork in the real cross-process connection-lock and FIFO metadata-verifier fixtures with fresh, independently initialized child interpreters. Preserve actual process/IPC behavior; do not suppress the multithreaded-fork warnings.
- [x] Preserve lock exclusion before release and acquisition afterward, FIFO rejection without opening a blocking stream, bounded startup/result waits and acknowledged child/IPC disposal on success and failure. Child targets must work without inherited closures or monkeypatch state.
- [x] Verify the three original warning cases with component-progress threads active and deprecations promoted to errors. Keep meaningful lock-bypass/FIFO-blocking controls, full applicable exact line/branch coverage, installed-package, license and public-boundary checks, independent adversarial and external review.

This is a test-fixture repair. Production authentication, process locking and metadata validation remain unchanged. Previously accepted unit launch and integration visibility behavior retain their acceptance; no live inference is required.

## Verification

Both affected files pass 179 tests under an asserted active production heartbeat with deprecations treated as errors; the actual launcher replay passes the same selection. Independent real-child controls reject lock bypass and FIFO blocking, preserve ordinary metadata behavior, and verify IPC/process cleanup after startup and child errors. The joined production runner passes 11,393 tests at 32 workers with exact 100% line/branch coverage (27,030 statements and 10,162 branches), zero warnings, and 333.59 seconds including collection and coverage. Installed-package checks, byte-identical rebuilt runtime wheel, licenses and public-boundary checks pass. External review found no fixture defect; its stale parent acceptance sentence is corrected alongside this record. Historical failed attempts remain retained and no warning-reproduction claim is inferred from isolated controls that did not reproduce the original warning.
