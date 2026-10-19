# Thread-safe process fixture startup

[Unit execution](implementation_plan_a9h.md)

- [ ] Replace explicit fork in the real cross-process connection-lock and FIFO metadata-verifier fixtures with fresh, independently initialized child interpreters. Preserve actual process/IPC behavior; do not suppress the multithreaded-fork warnings.
- [ ] Preserve lock exclusion before release and acquisition afterward, FIFO rejection without opening a blocking stream, bounded startup/result waits and acknowledged child/IPC disposal on success and failure. Child targets must work without inherited closures or monkeypatch state.
- [ ] Verify the three original warning cases with component-progress threads active and deprecations promoted to errors. Keep meaningful lock-bypass/FIFO-blocking controls, full applicable exact line/branch coverage, installed-package, license and public-boundary checks, independent adversarial and external review.

This is a test-fixture repair. Production authentication, process locking and metadata validation remain unchanged. Previously accepted unit launch and integration visibility behavior retain their acceptance; no live inference is required.
