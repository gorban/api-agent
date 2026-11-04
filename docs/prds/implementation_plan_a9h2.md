# Portable native unit execution

## Portable credential-helper fixture and timeout identity

- [x] Prepare the synthetic credential worker/helper before starting the short inference timeout, using bounded semantic readiness and a stdin admission handshake. Exercise delayed startup, timeout cleanup and an already-exiting worker without relaxing the production timeout or group-disposal assertions. Verify group liveness through the shared Linux/macOS process probe rather than treating absent Linux `/proc` files as macOS exit evidence.
- [x] Print the exact timed-out collected identity alongside its ordinal, elapsed time and limit. Credential-scrub that identity before console delivery; preserve deadline/status/coverage failure semantics.
- [ ] Human acceptance: rerun the full macOS base unit suite and the composed all-unit invocation. Separate outdated resource-pack fixture failures from the base deadline failure.

## macOS fixture follow-up and aborted-run evidence

- [x] Arm nested cleanup timeout injection only after its retained child signals readiness. Keep the real context, disposal receipt, authentication retention and retry assertions; isolate the scheduler hook to the owned control module and use bounded readiness, including delayed setup. Production deadlines remain unchanged.
- [x] Native lease tests use OS-issued reaped PIDs. Linux-specific uncertainty fixtures explicitly select their mocked proc filesystem on every host; do not interpret arbitrary out-of-range PID arguments or missing macOS proc files as evidence of process death.
- [x] Terminal unit execution prints credential-protected captured assertion/setup failures after runner finalization, even if pytest was aborted before its own summary. Reuse existing failure diagnostics and retain successful-stream levels. Omit per-test boilerplate for unstarted/incomplete tests without a captured failure.
- [ ] Rerun the actual macOS full suite to qualify these repairs and inspect any additional captured failures; passing Linux tests does not establish native macOS completion.

## Fresh native checkout and subprocess portability

- [x] Real GraphQL MCP unit proofs prepare locked child assets in their disposable fixture directory instead of depending on retained source `node_modules`. Preserve selected npm/node shim entry names when invoking managed executables. Packaging proofs separately verify current runtime bytes, source archive bytes, rebuilds and unsafe-input rejection without combining multiple builds into one watchdog body.
- [x] Process disposal uses the trusted system process inspector even under a helper-specific PATH, with bounded verification. A denied signal may be treated as exhausted only when the reserved unreaped child is visibly a zombie and every member of its group is a zombie; live, missing or uncertain evidence still rejects cleanup. Credential guardians preserve the helper exit code. Their terminal-state inspection has a separate bounded five-second cleanup budget while retaining the child reservation, including after an operation deadline; unavailable or uncertain inspection still rejects cleanup.
- [x] Canonicalize launcher-created Bedrock scratch directories before secure descriptor-relative writes. Retain no-follow validation for configuration, credentials and their parents; test a symlinked system temporary root through refresh, publication and cleanup.
- [x] Synthetic platform-specific credential tests explicitly select their intended branch. Allow bounded startup readiness independently of intentionally short validation deadlines.
- [ ] Human acceptance: fresh macOS full suite after these repairs. Prior interrupted results do not establish the status of tests that never ran.


## Native full-suite follow-through

- [x] Retain the actual child without reaping until its owned group has been disposed. Never signal a released numeric process-group ID. Reaped handles may verify terminal absence but cannot authorize another signal; cache absence on the exact handle to prevent retries. Preserve the actual evidence-worker PID for memory measurement, and reject uncertain ownership or live unowned groups.
- [x] Keep async signal delivery, pipe draining and reaping in one cancellation-owned cleanup scope. Repeated cancellation cannot skip the remaining teardown. Index-check subprocess disposal follows the same safety requirement without importing mutable checkout code.
- [x] Treat terminal selector notifications as advisory: nonblocking recovery streams must still drain available data and EOF when a notification is absent. Preserve input secrecy, cancellation and the final deadline check before releasing a helper result.
- [x] Reduce packaging proof subprocess overhead through bounded batched Git reads and immutable tree caching. Preserve exact current-worktree bytes, staged blob identity, committed workflow identity and a final live HEAD recheck. Share an actual built wheel between byte and installed-lifecycle proofs; retain actual source archive and rebuilt-wheel checks.
- [x] Descriptor leak assertions check actual owned descriptors rather than Linux procfs. Engine mocks intercept only engine commands; native boot/process probes remain real. Canonicalize fixture roots, assert platform-appropriate cache paths, and separate bounded startup readiness from intentionally short execution deadlines.
- [x] Full Linux unit gate: 26,691 tests pass at exact 100% line and branch coverage. Independent source reviews, causal rejection controls and staged public-boundary checks pass; publication retains committed-tree and metadata checks.
- [ ] Human acceptance: actual native macOS full suite, including terminal recovery, process teardown and installed packaging. Linux results and synthetic native branches do not establish that acceptance.

## Automatic managed-hook maintenance

- [x] Ordinary launcher commands upgrade exact known historical managed hooks automatically after validating the current template. No manual refresh or diagram-generation repair flags are prerequisites for unit execution.
- [x] Preserve custom, edited, symlinked and shared hooks. Reverify moved bytes and identity, publish exclusively into a vacant hook slot, and verify the published inode and bytes before releasing recovery data or reporting success. Restore or retain changed files on races and failure. Unknown hook markers never establish ownership.
- [x] Verify upgrades, idempotence, malformed/current-template rejection, concurrent edits and installations, rollback, full coverage and public boundaries.
- [ ] Human acceptance: pull and run the ordinary unit command on a checkout containing an older managed hook.

Verification: 26,704 full Linux unit tests pass at exact 100% line and branch coverage. Independent review closes the moved-file and published-file races; causal mutations reject omitted hash or identity checks. Native Mac acceptance remains open.

## Native process-inspection isolation

- [x] Scope synthetic subprocess factories to their owning consumer. Native process inspection must use the real system subprocess path, and helper cleanup must not add inspection children to a collection being retired. Exercise the system-ps backend on Linux as well as the native backend, with meaningful rejection controls.
- [x] Separate bounded helper readiness from intentionally short watchdog or renderer deadlines. Non-timeout selection and rejection tests use sufficient bounded setup time; retain production deadlines, actual timeout rejection and owned-group disposal.
- [x] Preserve safe native memory-query errno and verify the documented ABI. A live child with unavailable memory measurement remains rejected; do not infer a native permission failure is solved by a passing Linux fixture.
- [x] Complete independent and external review, full applicable coverage and public-boundary checks.
- [ ] Human acceptance: normal macOS full unit suite. Native memory-query unavailability followed by signal denial remains unresolved until a fresh native run; diagnostics now preserve the numeric query errno.

Verification: 26,719 full Linux unit tests pass at exact 100% line and branch coverage. Forced system-process inspection and readiness-only rejection controls pass. Native execution is a separate acceptance gate.

## Native boundary-test follow-through

- [x] Keep large conversation-index boundary tests independent of repeated schema-worker startup while retaining actual gate ordering, validation results and complete evidence. Real isolated validation remains covered separately.
- [x] Explicitly select the intended platform in cache-policy fixtures. Keep native process-inspection reads outside consumer-local read doubles and preserve the same response-byte limits.
- [x] Prove the cleanup fixture's actual retained child exit before injecting terminal signal refusal. Give ordinary recovery-semantic fixtures bounded startup allowance while preserving explicit short-deadline and privacy assertions. Production limits remain unchanged.
- [x] Complete independent and external review, full Linux unit coverage and public-boundary checks. Verification: 26,720 tests pass at exact 100% line and branch coverage.
- [ ] Actual macOS full-suite acceptance, including remaining schema/evidence availability, native memory-query exit timing and intentionally short-deadline fixtures. Passing Linux checks do not qualify those unresolved native results.

## Completed native-run diagnosis

- [x] Audit all cache-branch fixtures, including child execution and concurrent scratch ownership, against their intended platform; do not write into the active user's caches.
- [x] Reproduce shared schema-worker and process-startup availability failures under controlled resource limits before changing scheduling or execution limits. Preserve explicit worker overrides and production timeout/rejection semantics.
- [x] Reconcile native task disappearance with positive retained-child terminal evidence. An unavailable memory sample alone must not authorize successful release or skip live-worker enforcement.
- [ ] Complete source review, causal rejection checks, full coverage and publication checks; retain actual native qualification as a separate gate.

- [x] Viewer shutdown must reject accepted connections whose handlers begin after shutdown's client snapshot. Prove the real accepted-transport/queued-handler race without extending request or cleanup deadlines, and retain writer closure and empty client ownership on completion.

[Retained child exit and final release](implementation_plan_a9h3.md) defines the native terminal proof and post-cleanup credential release requirements.
