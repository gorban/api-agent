# First application E2E execution slice

[E2E requirements](implementation_plan_a3.md) · [Prepared sessions](implementation_plan_i2c.md) · [Confirmation attempts](implementation_plan_d5.md)

The first executor uses the existing selected-test runner, shared authentication,
prerequisites, suite exclusion, cancellation, result presentation and immutable
Allure recording. Integration execution keeps its existing mode and one-pass
semantics. E2E records its own operation identity and recollects only E2E suites.

- [x] Require explicit picker continuation and E2E acceptance before execution.
  Noninteractive execution requires explicit START=E2E; START=True does not
  grant E2E acceptance. Discovery and declined acceptance start no prerequisites.
- [x] Require every admitted E2E test to consume the prepared UI session. Always
  launch its frontend; DEBUG or E2E_DEBUG shares headed/retained ownership across
  UI and LLM setup. Debug selections must be a strict subset of the whole suite.
- [x] Execute the included Studio graph test through browser submission, actual
  Agent HTTP routing, the launch-admitted selected application graph and selected
  LLM, then await the displayed answer and bounded turn capture attempt before completion.
  Publish its screenshot when the safety policy permits; otherwise retain explicit
  unavailable evidence under the existing turn-capture contract.
  Preserve normal graph journals and Allure user-turn ownership.
- [x] Verify mode isolation, rejection before side effects, stop/cleanup,
  recording identity and actual graph traversal with controlled fixtures; complete
  unit coverage, packaging/boundary checks and independent review.
- [x] Prove one explicitly selected live Studio E2E with graph evidence and
  per-turn screenshots in a normally published Allure result.
- [x] Complete external review of the merged executable batch.
- [ ] Human review the bounded E2E selection, rendered response and matching
  turn screenshot, node payloads and LLM evidence in Allure.

This slice accepts E2E_RERUNS=0 only. A positive allowance fails preflight before
services or authentication. Defaults remain unchanged: select zero explicitly
until the confirmation aggregation in the linked requirements is delivered.
Retry/rate reporting, full scenario migration and broader UI providers remain
pending. This checkpoint does not complete the parent E2E checklist.
