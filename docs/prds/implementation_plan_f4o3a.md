# Progressive display reasoning

[Reasoning carriers and Studio](implementation_plan_f4o3.md) ·
[Streaming failure diagnostics](implementation_plan_f4o3a1.md)

Expose provider-returned plaintext reasoning as progress, separately from the final
answer. It is model output, not a verified explanation or a generated friendly
summary. Preserve the existing `reasoning_token` SSE and `reasoning_summary`
terminal names for compatibility; the latter carries admitted display reasoning
as well as provider summaries. Never decrypt or display opaque continuation state,
signatures, or undocumented fields.

- [x] Admit native plaintext Completions reasoning fields and completed Converse
  reasoning text. Explicitly interpreted tagged Completions envelopes may also
  supply display reasoning; literal or ambiguous markup must not be interpreted.
- [x] Preserve credential protection across reasoning, answer and tool carriers,
  including fragmented credentials, encoded values and bounded memory/work.
- [x] Forward native summary/reasoning prefixes during supported streams. Hold
  ambiguous carriers and credential-sized tails until they can be validated.
  Validate cumulative progress against the authoritative completed result.
- [x] During ordinary text calls with an active progress consumer, consume a
  provider's advertised stream without exposing draft answers as graph results.
  Rich requests retain their existing format/fallback negotiation; nonstreaming
  calls publish admitted reasoning after completion, before later graph calls.
- [x] Keep the progress sink request-local and composable: another application
  can translate admitted text into friendly messages without changing the
  provider or Studio wire contract. `ReasoningProgress` identifies the call
  (using its request ID or generating one when omitted),
  distinguishes incremental text from a completed call's full text, and excludes
  content from its repr. Prevent duplicate completed-call delivery.
- [x] Verify intermediate delivery while a later workflow call is blocked,
  final-answer separation, cancellation, cleanup and limits. Include a normal
  selected-provider Studio run and inspect its admitted reasoning evidence.
- [x] Separate provisional failed-attempt progress from authoritative completed
  sections. Preserve text within a call and add paragraph boundaries between
  calls. Concurrent completed-call consumers share one success/error receipt.
- [x] Close applicable unit coverage and independent/external review before
  human acceptance. Do not require every model to produce reasoning.
- [ ] Human acceptance: inspect reasoning between calls, the settled final
  answer and collapsed details on a selected supported route.

Friendly translation is optional application behavior and can incur inference.
Optional declarative summaries and replay preservation follow the
[node summary requirements](implementation_plan_f4o3b.md).
This work does not add it to Studio or replay display reasoning as conversation
history. Logging and tracing retain their separate content controls.
