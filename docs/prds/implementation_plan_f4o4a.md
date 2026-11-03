# Converse reasoning and native continuation

[Reasoning controls](implementation_plan_f4o4.md)

- [x] Qualify documented GPT/Claude model IDs and regional profiles for native
  Converse effort controls. Use the existing shared low/medium/high intent; map
  off only where disabling reasoning is documented. Unknown models remain
  unknown rather than acquiring capabilities from display-name parsing.
- [x] Map GPT native effort/optional summaries and Claude adaptive thinking plus
  native output effort through additionalModelRequestFields. Preserve provider
  defaults when effort is omitted. Validate the actual worker payload before
  side effects and enable Studio choices only from provider metadata.
- [x] Provide provider-contributed LLM_REASONING_LOW_TOKENS=1024,
  LLM_REASONING_MEDIUM_TOKENS=4096 and LLM_REASONING_HIGH_TOKENS=16384 for budget-only
  Claude versions. Keep them configurable and conditionally visible on Converse;
  validate monotonic positive presets, cap below the effective total output
  ceiling with 1024 answer tokens reserved, and reject insufficient ceilings.
  Adaptive native-effort models ignore these manual-budget settings. Opus 4.5 composes its native output effort with the required manual thinking budget, so both are sent. Preserve explicit
  model/endpoint/API/account selection and application retry counters.
- [x] Transport documented binary redactedContent using a bounded canonical
  base64 representation only in its named native union. Restore exact SDK bytes
  on replay. Reject malformed/mixed unions, invalid encodings and exhausted
  bounds; do not stringify arbitrary SDK objects or discard native state.
- [x] Preserve signed/plaintext and opaque blocks in original order through
  ordinary answers, tool calls/results and later requests. Match replay to its
  visible answer and tool calls, model, route, credential snapshot and owner.
  Opaque bytes/signatures never enter UI or optional logs/traces/evidence.
- [x] Keep displayable reasoning separate from final answers; preserve omitted
  summaries and unknown reasoning-token usage honestly. Verify buffered Converse
  progress before graph completion without claiming token-by-token streaming.
- [x] Preserve worker deadlines, cancellation, cleanup and protected diagnostics.
  Prove serialization, tool continuation and credential rejection causally;
  finish exact unit coverage, installed/package/public-boundary and external
  review before the human checkpoint.
- [ ] Human acceptance: GPT Converse requests complete with selected effort;
  supported Claude efforts appear and work, budget-only choices use the configured
  values, and tool continuations complete without displaying opaque state.

## Public contracts

- [Reasoning union](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ReasoningContentBlock.html)
- [GPT Runtime controls](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html)
- [Claude adaptive effort](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)
- [Claude thinking budgets](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)
