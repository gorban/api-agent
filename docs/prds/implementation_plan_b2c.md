# B2c — Useful API demonstration

## Requirements

- [x] B2.8 Evolve only the main demo into planning/optional GraphQL/rendering.
  Verify query validity deterministically and relevance with an LLM before
  execution. Correctness/grounding block every pass; preferences may relax only
  on the final pass. Prove that behavior separately with controlled deterministic
  integration tests; never relax the permanent contrived fixture.
- [x] B2.9 Use published GraphQL MCP with a locked dependency tree, bounded
  owned lifecycle and schema tools. Keep the tool seam API-independent; prove an
  unchanged consumer works with synthetic REST. Generation cannot execute
  queries before their verification boundary.
- [x] B2.10 Select a read-only synthetic API Compose profile only for the bundled
  demo requiring API execution with omitted/blank endpoint. Remote endpoints
  and external packs must not start it. Own loopback ports/readiness/cleanup and
  render partial GraphQL errors and transport failures faithfully.

## Acceptance

- [x] Deterministic rejection of invalid queries before any execution.
- [x] Relevance/grounding guards remain blocking on final pass.
- [x] Same consumer works unchanged with synthetic GraphQL and REST tooling.
- [x] Synthetic API lifecycle and endpoint selection tests pass.
- [x] Error rendering, dependencies and installed artifacts verified.
- [x] Independent review and applicable 100% line/branch unit gate complete.

The selected `demo/simple/flow.md.html` is implemented. Its controlled regression is
`tests/integration/test_useful_demo_graph.py`; it compiles the actual declaration,
checks final-pass guards and verifies tool ordering and partial-result rendering.
Controlled integration, installed/relocated artifacts, the complete unit and coverage
gate, and independent/external reviews pass. Human acceptance remains deferred;
controlled evidence does not establish live acceptance.

- [ ] Human review the completed API demonstration with the selected provider.

## Phase-one controlled proof

`tests/integration/test_workflow_preferences.py` uses an actual compiled Workflow,
source-matched deterministic functions and a temporary synthetic pack. It proves
preference=false rejects pass one and accepts the final pass when truth=true;
truth=false rejects every pass and exhausts the component. Both tests publish
structural evidence and require no LLM fixture. Unit replay tests cover both cases;
focused mutations removing truth blocking or final-pass relaxation fail them.
This is a separate engine behavior proof, not completion of the useful API demo.

## Package and endpoint decisions

- [x] Before dependent implementation, resolve the [MCP package, tool boundary and endpoint-selection decisions](implementation_plan_o.md) with the user. Lock the agreed public npm package and dependency tree; distinguish the demo's synthetic target from a universal external-pack fallback.
- [x] Acquire the synthetic API through [pack-declared prerequisites](implementation_plan_o1.md). Demonstrate the GraphQL tool against that target without requiring a particular database engine. Preserve query-verification, partial-error and lifecycle acceptance above.

- [x] Show the bundled `API_ENDPOINT` default in configuration Review. Allow editing it to a nonempty remote endpoint and clearing its saved `.env` value to restore the blank local synthetic API default.

## Reviewer tool evidence

- [x] Supply the reviewer the same selected tool name, description and argument schema used by the planner, plus the successful owner pre-execution validation result. Obtain this from the prepared call passed to the relevance callback; do not add undeclared tool operations or require the user to supply the API schema. Treat the specification as data, preserve original-request authority and do not imply that a validated query has executed or returned data.
- [x] Keep tool context stable in the reviewer history across retries. Preserve correctness, grounding and relevance rejection, query validation before reviewer admission, and optional no-tool behavior. Verify positive context delivery and independent rejection controls, then exercise the actual selected graph and trace its tool invocation with an authorized provider.
