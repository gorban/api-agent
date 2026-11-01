# Runtime Completions reasoning carriers

[Bedrock Runtime requirements](implementation_plan_f4o.md)

Qualify final-answer extraction for Runtime Chat Completions responses that carry
reasoning inside text rather than a separate response field. Successful discovery
or an HTTP success alone does not establish final-answer compatibility.

- [ ] Establish the upstream format contract and its applicability to endpoint,
  API, model identity and response shape before interpreting in-band markers.
  Preserve explicit operator selections; do not change API or endpoint implicitly.
- [x] Prefer native reasoning fields across model families. Text-only common
  `<reasoning>` and `<think>` prefixes are ambiguous with literal output: default
  `BEDROCK_COMPLETIONS_REASONING_FORMAT=auto` must reject them with actionable
  configuration guidance. Explicit `tagged` interprets the leading envelope;
  `literal` preserves the entire answer. No model-family whitelist or implicit
  endpoint change establishes this interpretation. Never promote raw text from
  either envelope to a displayable reasoning summary.
- [x] Separate protocol-defined reasoning from the visible final answer without
  treating arbitrary user-authored markup as a reasoning carrier. Preserve literal
  answer text, structured output, tool calls and their continuation semantics.
- [x] Cover complete and fragmented streaming carriers, absent or malformed
  delimiters, incomplete output, cancellation and bounded memory. Retain safe error
  diagnostics and usage; unknown reasoning-token splits and costs stay unknown.
- [x] Apply the existing content and credential policies to captured wire evidence.
  Preserve the original response as allowed evidence independently of final-answer
  normalization; do not weaken exact-answer integration assertions.
- [x] Protect displayable summaries with the provider credential authority before
  release, including credentials split across summary/answer or summary/tool
  value carriers, under the same bounded work limit.
- [x] Verify source and installed/relocated behavior, exact unit line/branch coverage,
  meaningful rejection controls, independent/external review and an authorized
  normal Studio E2E before qualifying this response shape for application use.
- [ ] Human acceptance of visible answers, content-enabled evidence and reasoning
  controls on the qualified route.

The [AWS OpenAI response documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-openai.html)
describes tagged reasoning for InvokeModel. Confirm the applicable Chat Completions
contract rather than applying that rule to unrelated text responses.

## Studio display of provider-supplied summaries

- [x] Accept only separately supplied displayable summary text: optional progressive SSE `reasoning_token` content and an optional authoritative terminal `reasoning_summary` string. Keep it separate from final answer text, and never parse markup or infer a summary from hidden, encrypted or raw reasoning.
- [x] Show an animated Thinking state while awaiting answer text, respecting reduced-motion preference. Show progressive summary as transient plain text kept only in memory; replace that view with the final answer when it arrives. Persist only the authoritative terminal summary in collapsed response details. Errors and cancellation clear the pending indicator and transient summary.
- [x] Wait for terminal stream completion, the settled final DOM and completion of finite message animations before normal Studio E2E answer assertions and screenshots. Pending summary updates must not restart the message arrival fade.
- [x] Qualify the provider's safe summary contract and backend event/result mapping, including explicit content policy and no hidden reasoning disclosure.
- [ ] Human acceptance: inspect pending, final, and collapsed-summary states on an authorized Studio turn.
