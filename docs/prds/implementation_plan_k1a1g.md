# Readable terminal failure status

[Terminal classification](implementation_plan_k1a1e.md) · [Failure evidence](implementation_plan_k1a1f.md)

- Publish an explicit failure or partial outcome as an OpenTelemetry error
  status with a readable description. Escaping exceptions also supply a bounded
  `api_agent.failure` event on the final request and failed workflow scopes.
  Preserve cause/call correlation; do not invent exception facts for a returned
  typed outcome.
- Fixed diagnostic summaries remain available without content permission.
  Detailed admitted provider fields require trace content permission and live
  credential scrubbing before SDK recording. Never format arbitrary exceptions,
  export stacks, or include candidates or opaque reasoning in status messages.
- Freeze new credential registration with the shared evidence lease from
  projection through SDK publication, releasing on every exit. Re-scrub cached
  descriptions before subsequent writes; prior publication cannot be recalled.
- Preserve the description when subsequent scope closure updates status.
  Duplicate reporting of the same admitted details must not duplicate events.
  Only the exact terminal outcome selected by the request owner may supply its
  failure description; secondary errors cannot overwrite the retained cause.
  Recovered child failures must not fail the final request; cancellation remains
  cancellation. Observations cannot change execution, deadlines or retries.
- Do not invent an assistant answer for a failed operation. Existing native
  response evidence remains intact; failure status is a separate diagnostic.
- A local response-body bound must name its byte ceiling, state that decoding
  did not complete, and preserve unknown execution/usage. It is distinct from
  model output-token limits; no bound increase or automatic replay is implied.

## Acceptance

- [x] Prove status/event projection and preservation with both destination
  configurations, content permissions, credentials, recovery and cancellation.
- [x] Prove response-size rejection before JSON/SSE decoding with no extra call.
- [x] Complete causal rejection controls and applicable repository gates.
- [x] Human acceptance sees the final failure in the selected trace viewer.

Standard OTEL status descriptions also supply Langfuse status messages through
its [documented fallback](https://langfuse.com/docs/observability/features/log-levels).
