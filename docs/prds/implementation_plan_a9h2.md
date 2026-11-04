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
- [x] Process disposal uses the trusted system process inspector even under a helper-specific PATH, with bounded verification. A denied signal may be treated as exhausted only when the reserved unreaped child is visibly a zombie and every member of its group is a zombie; live, missing or uncertain evidence still rejects cleanup. Credential guardians preserve the helper exit code.
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
