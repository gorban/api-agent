# OpenRouter native reasoning admission and retained diagnostics

[Effort controls](implementation_plan_f4o3b2.md) ·
[Testing guide](../testing_openrouter_reasoning.md)

An accepted reasoning effort does not qualify a native response format. A
successful upstream answer must remain usable when an independently qualified
route returns encrypted reasoning alongside it.

- [x] Reproduce the Terra 6.1 Responses puzzle rejection with bounded selected-account
  inference and retain status, usage and response structure separately from content.
- [x] Qualify `openai/gpt-6.1-terra` native Responses parsing and exact ordered
  continuation replay. Reuse existing bounds, credential checks and opaque-state
  exclusion from evidence and browser payloads. Keep answer text separate.
  Prove node retries retain the native assistant payload until invocation cleanup,
  preserve its wire prefix and begin the next user turn with fresh retry history.
- [x] Separate native-state admission from visible-reasoning capability. Encrypted
  reasoning is not readable text; do not advertise or synthesize a native explanation
  from ciphertext. Actual validated plaintext, when returned opportunistically,
  remains displayable; a catalog capability is not a filter discarding received text.
  Do not inherit native-state qualification from effort version ranges.
- [x] Log retained integration bridge execution errors to stderr with credential
  scrubbing and stack locations. Use the current manual owner's registered authority;
  withhold arbitrary messages without it. Preserve diagnostics regardless of
  `LOG_CONTENT`. Do not add request/response dumps to this diagnostic path.
- [x] Verify live puzzle completion and two-call replay, normal Studio integration,
  causal rejection checks, exact applicable unit coverage, packages, public boundary
  and independent external review before publication.
- [ ] Human acceptance of Terra puzzle completion and actionable retained diagnostics.

Manual DEBUG requests remain outside finalized Allure results. This repair does
not create a manual tracing owner or promise token streaming or readable reasoning.
Broader trace semantics remain in [complete execution traces](implementation_plan_k1a1.md).

Public contract: [OpenRouter reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
