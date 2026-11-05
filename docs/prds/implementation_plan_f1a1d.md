# Deterministic candidate repair

[Declarative responses](implementation_plan_f1a1.md) · [Ordered history](implementation_plan_f1a1c.md)

## Requirements

- [x] Feed definitive generated-output validation failures back to their original
  producing node, including JSON, schema, size and authored cross-field rules.
  A typed candidate rejection identifies repairable data; arbitrary exceptions,
  invalid configuration, immutable input, authentication, cancellation, expired
  deadlines and unavailable validators remain terminal.
- [x] Retry the rejected producer within its owning component's existing total
  pass budget and inherited deadline. Deterministic repairs and semantic guard
  retries share that budget. Repair malformed reviewer output at the reviewer,
  without regenerating a valid candidate. Do not invent retries for one-pass
  flows or stateful cycles without a declared retry controller.
- [x] Preserve successful prefix work, actual assistant messages and opaque
  continuation. Append credential-safe constraint details, paths, limits and
  observed sizes to the same node's ordered dialogue. Keep original instructions
  authoritative and avoid duplicating earlier messages or fabricated replies.
- [x] Block restart after effect dispatch, including uncertain dispatch. Native
  tool conversations may repair remaining proposals or final text within their
  existing round limit while retaining actual tool results and completed-input
  protections. Never automatically repeat a completed tool operation.
- [x] Advertise the bundled planner's 8,192-character answer constraint in its
  schema and provide actionable rejection details for additional native rules.
  Keep invalid candidates out of accepted workflow state.

## Acceptance

- [x] Audit every included flow and native retry loop; document controllers and
  non-applicable one-pass/stateful cases rather than claiming implicit coverage.
- [x] Prove overlength and schema repair, reviewer-only correction, shared budget
  exhaustion, nested isolation, exact feedback/history ordering and preservation
  of completed tool work. Reject programming, authority, timeout and uncertain
  execution failures without additional inference.
- [x] Complete causal rejection checks, exact applicable unit line/branch
  coverage and independent adversarial/external review.
- [ ] Human review: observe a deterministic rejection followed by a corrected
  producer response, with separate failed-attempt and final workflow evidence.

## Verification

All 27,072 unit tests pass with exact coverage of 63,078 executable lines and
24,904 branches across 382 production files. Controlled native/provider and
worker-wire checks preserve original rejected responses, usage and opaque state;
causal controls detect replay, budget, authority, diagnostic privacy and schema
eligibility regressions. Independent adversarial and external source review pass.
The normal recorded integration runner passes all six useful-demo cases,
including deterministic length repair and pre-completion reasoning delivery.
These checks do not guarantee that a selected provider will produce a correct
answer within the declared pass limit. Cross-node ambiguity, one-pass/stateful
flows without a retry controller and uncertain effect execution remain terminal.
