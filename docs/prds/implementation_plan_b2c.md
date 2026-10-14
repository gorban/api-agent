# B2c — Useful API demo, phase two pending

## Requirements

- [ ] B2.8 Evolve only the main demo into planning/optional GraphQL/rendering.
  Verify query validity deterministically and relevance with an LLM before
  execution. Correctness/grounding block every pass; preferences may relax only
  on the final pass. Prove that behavior separately with controlled deterministic
  integration tests; never relax the permanent contrived fixture.
- [ ] B2.9 Use published GraphQL MCP with a locked dependency tree, bounded
  owned lifecycle and schema tools. Keep the tool seam API-independent; prove an
  unchanged consumer works with synthetic REST. Generation cannot execute
  queries before their verification boundary.
- [ ] B2.10 Select a read-only synthetic API Compose profile only for the bundled
  demo requiring API execution with omitted/blank endpoint. Remote endpoints
  and external packs must not start it. Own loopback ports/readiness/cleanup and
  render partial GraphQL errors and transport failures faithfully.

## Acceptance

- [ ] Deterministic rejection of invalid queries before any execution.
- [ ] Relevance/grounding guards remain blocking on final pass.
- [ ] Same consumer works unchanged with synthetic GraphQL and REST tooling.
- [ ] Synthetic API lifecycle and endpoint selection tests pass.
- [ ] Error rendering, dependencies and installed artifacts verified.
- [ ] Independent review and applicable 100% line/branch unit gate complete.

No phase-two implementation or live acceptance is claimed here.

## Phase-one controlled proof

`tests/integration/test_workflow_preferences.py` uses an actual compiled Workflow,
source-matched deterministic functions and a temporary synthetic pack. It proves
preference=false rejects pass one and accepts the final pass when truth=true;
truth=false rejects every pass and exhausts the component. Both tests publish
structural evidence and require no LLM fixture. Unit replay tests cover both cases;
focused mutations removing truth blocking or final-pass relaxation fail them.
This is a separate engine behavior proof, not completion of the useful API demo.
