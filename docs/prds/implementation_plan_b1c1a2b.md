# Conversation, turn and invocation reporting

[Complete flow-aligned evidence](implementation_plan_b1c1a2.md)

Preserve separate graph executions beneath their actual user turns and conversations. Aggregate costs from authoritative unique calls while retaining test-wide totals and the existing canonical payload files. Reporting must not imply conversation-state isolation merely because identifiers differ.

## Execution identity

- [x] Add explicit canonical conversation identities to chat requests without changing the existing per-request session identity. The bundled UI retains its conversation identity for follow-ups and creates a new one for New chat. Legacy callers may omit conversation identity; do not infer it from matching messages, model, URL or timing.
- [x] Propagate trusted conversation/trial context and the actual turn identity through both parsed-flow executors, observations, call evidence and durable journals. Preserve separate invocation, visit, attempt and call identities. Restore context after success, failure or cancellation; concurrent conversations must not share ambient state.
- [x] Record independent qualification executions as numbered trials, not continuing conversations. Preserve legacy journals and payload hydration without inventing missing grouping metadata. Validate new metadata and reject conflicting attribution.
- [x] Admit supplied journal context at invocation: bind an unstarted unlabelled journal to the active context, inherit an explicit supplied context when ambient context is absent, and reject conflicting or retroactively changed evidence before execution. Validate every non-omitted turn identity, including empty strings.

## Native hierarchy and totals

- [x] Present conversation -> numbered user turn -> GRAPH invocation -> component -> attempt/node/call, with stable identifiers retained as parameters. Multiple invocations within one user turn remain separate. Trial wrappers identify independent coverage executions. Legacy ungrouped evidence remains readable.
- [x] Roll tokens, list-price estimates and independently qualified reported charges up from unique frozen call identities to turn and conversation. Preserve test and suite totals, including calls outside any conversation. Include explicitly attributed calls outside GRAPH in their turn without inventing graph ownership. Never sum both parent totals and their children.
- [x] Preserve unknown/partial attribution and unavailable rates/charges as explicit states. Do not label quota-based allocations as measured per-conversation charges. Preserve provider/model/version pricing distinctions, payload single-copy storage and actual execution outcomes.
- [x] Propagate native terminal-error/red and internal-retry/yellow attention through turn and conversation ancestors while preserving unaffected siblings and canonical test outcomes.

## Acceptance

- [x] Deterministic production-path tests cover two turns in one conversation, a second conversation, multiple graph invocations in one turn, independent trials, failure/cancellation, duplicate delivery, conflicting identity and legacy history hydration.
- [x] Verify new-chat request history excludes the previous chat; assert actual state/input separation in addition to distinct labels. Prove exact-once rollups with calls outside GRAPH and outside conversations, mixed rates and unavailable evidence.
- [x] Complete applicable exact unit coverage, package/relocation and boundary checks, independent implementation review and external review.
- [ ] Human inspection confirms grouping, costs and retry/error ancestor attention. Historical overlay UI remains a separate requirement.

**Deferred human review**

Implementation and required automated, independent and external reviews are complete. Only the explicit human acceptance above remains unchecked.
