# Recorded workflow execution contract

[Execution evidence](implementation_plan_b1c1.md) · [Source identity](implementation_plan_b1c3.md) · [Reporting opt-out](implementation_plan_d3.md)

This child records the supported generic workflow runtime. It does not claim application graph equivalence or historical lookup.

## Selected definition and event ownership

- [x] Emit a versioned projection of admitted graph, ordered steps, resolved replacement identities, resource hashes, and every declared or implicit route. Resource paths are logical owner paths; projection and journal contain no absolute checkout paths or declaration text.
- [x] Record actual START, sequential, conditional and END routing at execution time. Capture nested scope start/end, guard attempts, outcome, shared timeout provenance and monotonic elapsed time. Owner-assigned event sequence and IDs are retained before IPC.
- [ ] Bind every node visit, component attempt, provider/tool leaf call, turn and invocation to unambiguous parent/cause IDs. Current v2 IDs and terminal provider call joins are implemented; richer stateful/resume and transport retry identities still require joined review. Unowned/missing calls are partial. Preserve requested/resolved/actual selection and charge versus estimate basis from frozen accounting.
- [ ] Prove that event loss, malformed ordering, missing terminal events and buffer overflow prevent any exact complete-route claim. Current v2 causal scope/route/call checks reject orphan parents, late calls, omitted visits and false completion; nested failure/cancellation and stateful paths still need joined verification.

## Privacy and publication

- [x] Retain bounded structural fields and owner-classified, schema-declared plain input/output scalars; scrub each complete admitted scalar before its 512-character display limit and before journal memory or runner IPC. Reject classified string inputs above 65,536 characters. `LOG_CONTENT` other than explicit True records a withheld reason. Unsafe credential, continuation and hidden reasoning field names are excluded even when True. A longer scrubbed string records `reason=truncated` and its field name in `truncated_fields`, while a scrubbed value of exactly 512 characters records `reason=captured`.
- [x] Verify journal digest at the runner boundary and attach projection and events through the existing Allure result/manifest settlement. Native scope steps use the same event source; a separate manifest-hashed accounting join settles call IDs and frozen leaf/inclusive cost parameters without double counting.
- [ ] Verify each consumed declaration/resource against committed owner provenance before any reporting-enabled graph effects, including staged, unstaged, untracked and ignored files and installed resources. Build-time Git-object sidecars allow offline installed verification of selected physical documents; inline prompts derive from their admitted parent. Native Python retains separate callable identity rather than joining the document hash policy. Explicit reporting skip must disable Allure entirely. Joined end-to-end acceptance remains.

## Acceptance

- [ ] Synthetic producer-to-sanitizer-to-IPC-to-Allure tests cover nested retries, repeated visits, calls, errors, cancellation, dropped events and bounded privacy. Run installed/relocated and exact applicable coverage gates, then independent review. Existing pilot fingerprints remain historical evidence; any changed execution behavior needs separately approved calibration.
