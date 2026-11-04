# Remaining native readiness and coverage controls

[Concurrency qualification](implementation_plan_a9h5.md)

- [x] Preserve the selected worker policy and production deadlines while separating fixture startup from the behavior under test. Establish actual helper/descendant, connection and tracing prerequisites before inducing timeout, retry, interruption or cleanup faults. Retain real process ownership, portable liveness, span termination, privacy and exact retry assertions.
- [ ] Investigate concurrent connection-store failure without weakening private-file permissions, ownership, hardlink, symlink or serialized refresh controls. Distinguish reproduced defects from unavailable native diagnostics before altering production behavior. Preserve the stable connection error code and suppressed upstream context while attaching only a fixed file-check stage and bounded exact-integer errno; omit paths, identifiers, permissions values and arbitrary exception text. Preserve the primary file-check diagnostic if secondary descriptor cleanup raises; never retry an ambiguous close, and retire the opened file if owned-directory closure fails before return. Normalize directory-acquisition failures through the same safe boundary while preserving its existing checks. Diagnostic instrumentation does not establish repair of an unreproduced native failure.
- [x] Deterministically exercise recovery non-terminal read-error and post-EOF deadline guards, registered viewer-client cancellation/drain and evidence-listener accept failure. Preserve separate late-handler coverage. Assert meaningful outcomes and independently mutate each rejection path.
- [x] Complete independent adversarial and external source review, full applicable unit line/branch coverage, workflow and staged public-boundary checks. Require committed metadata verification before normal publication.
- [ ] Native normal-launcher acceptance, including exact coverage; local verification alone does not establish it.

Independent adversarial and external source review pass, including causal controls for file-check rejection, bounded diagnostics and client cleanup. The native concurrent connection-store cause remains unresolved; diagnostic notes identify a recurrence without weakening validation. Native acceptance remains open.

Final Linux verification: 26,845 unit tests pass at 16 workers with exact 100% line and branch coverage. This supersedes the in-flight run; it does not establish native acceptance or resolve the concurrent native connection-store cause.
