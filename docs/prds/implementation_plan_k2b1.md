# Native terminal interruption and cleanup status

[Local cleanup requirements](implementation_plan_k2b.md)

- [x] Reproduce Ctrl+C as a real foreground process-group interrupt with a native managed Playwright browser. Preserve the reported pipe-closure diagnostic as evidence; synthetic owner cancellation alone does not establish this behavior.
- [x] Retire a failed browser transport only after independent driver/process/endpoint evidence proves its owned resources terminated. Preserve live, unrecognized and cancellation-resistant owners as pending; do not swallow arbitrary close failures or force process exit.
- [x] Print initial stopping and pending status, with the cleanup scope once per change. Repeated retries must not alternate full stopping/pending blocks. While pending, give a concise reminder no more often than every 15 seconds; meaningful scope changes remain immediately visible.
- [x] Verify one Ctrl+C, repeated interruption, disconnected transport, normal close and genuine retained cleanup failures. Use causal controls, applicable exact line/branch coverage, native and installed checks, independent and external review before delivery.
- [ ] Human retest: run the noninteractive local operation, stop once with Ctrl+C and confirm resources close without repeated pipe errors or scope spam. If a genuine callback remains pending, retain the creating loop and visible bounded retries.

## Verified checkpoint

A real foreground process-group interrupt reproduces the failure and verifies the
repair through both source and installed terminal/controller/browser composition.
All 14,269 unit tests pass in one full run with exact 100% line/branch coverage.
Independent source/causal, package, boundary and external reviews are complete.
The original running process was neither injected nor signalled; human retest
of a fresh operation remains unchecked.
