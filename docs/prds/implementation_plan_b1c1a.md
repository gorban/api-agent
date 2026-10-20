# Recorded workflow execution contract

[Execution evidence](implementation_plan_b1c1.md) · [Source identity](implementation_plan_b1c3.md) · [Reporting opt-out](implementation_plan_d3.md)

This child records the supported generic workflow runtime. It does not claim application graph equivalence or historical lookup.

## Selected definition and event ownership

- [x] Emit a versioned projection of admitted graph, ordered steps, resolved replacement identities, resource hashes, and every declared or implicit route. Resource paths are logical owner paths; projection and journal contain no absolute checkout paths or declaration text.
- [x] Record actual START, sequential, conditional and END routing at execution time. Capture nested scope start/end, guard attempts, outcome, shared timeout provenance and monotonic elapsed time. Owner-assigned event sequence and IDs are retained before IPC.
- [x] Bind supported v2/stateful node visits, component attempts, terminal provider calls, turns and invocations to parent/cause IDs; independently review nested stateful failure/cancellation and process-local next-turn continuation. Unowned/missing calls are partial. Preserve selection and frozen charge/estimate basis.
- [ ] Extend and independently verify transport retry identities and richer suspension/resume semantics when those capabilities are implemented; normal-entry continuation does not establish arbitrary mid-node resumption.
- [x] Prove event loss, malformed ordering, missing terminal events and overflow prevent an exact complete-route claim, including supported stateful paths and nested failure/cancellation. Reject orphan parents, late calls, omitted visits and false completion.

## Privacy and publication

- [x] Retain bounded structural fields and owner-classified, schema-declared plain input/output scalars; scrub each complete admitted scalar before its 512-character display limit and before journal memory or runner IPC. Reject classified string inputs above 65,536 characters. `LOG_CONTENT` other than explicit True records a withheld reason. Unsafe credential, continuation and hidden reasoning field names are excluded even when True. A longer scrubbed string records `reason=truncated` and its field name in `truncated_fields`, while a scrubbed value of exactly 512 characters records `reason=captured`.
- [x] Verify journal digest at the runner boundary and attach projection and events through the existing Allure result/manifest settlement. Native scope steps use the same event source; a separate manifest-hashed accounting join settles call IDs and frozen leaf/inclusive cost parameters without double counting.
- [x] Verify each consumed declaration/resource against committed owner provenance before any reporting-enabled graph effects, including staged, unstaged, untracked and ignored files and installed resources. Build-time Git-object sidecars allow offline installed verification of selected physical documents; inline prompts derive from their admitted parent. Native Python retains separate callable identity rather than joining the document hash policy. Explicit reporting skip must disable Allure entirely. Controlled source/installed reporting checks pass; human acceptance remains.

## Acceptance

- [x] Synthetic producer-to-sanitizer-to-IPC-to-Allure tests cover supported nested attempts, repeated visits, calls, errors, cancellation, dropped events and bounded privacy. Exact applicable coverage, source/relocated installed artifacts and independent runtime/artifact review pass.
- [ ] Complete external review and human acceptance; transport retries and richer suspension/resume retain their separate future scope.

## Installed reporting verification

Normal reporting-enabled installed execution verifies admitted source before native imports and saves manifest-hashed projection, journal and accounting artifacts plus nested Allure steps. Controlled HTTP calls reconcile once each between accounting and node visits; unknown cost remains unknown. Missing/duplicate calls and mismatched turns remain partial. Altered profiles and corrupt committed receipts reject before native import. These checks establish the recording seam, not live diagnostics, historical viewing or full application acceptance.
