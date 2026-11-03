# Bedrock streaming failure diagnosis

[Progressive display](implementation_plan_f4o3a.md)

- [x] Preserve stream decoder rejection codes, observed usage and a bounded,
  credential-checked diagnostic across the worker boundary. Identify the known
  event type, sequence and local validation location without logging raw SDK
  messages, response bodies, secrets or opaque reasoning state. This must work
  when optional content capture is disabled.
- [x] Distinguish an HTTP-success decode failure from authentication or permission
  rejection. Do not retry a possibly executed inference as a pre-dispatch failure.
- [x] Accept a geographic profile/underlying model alias transition only when
  both IDs satisfy the existing selected-target check. Preserve the actual
  terminal ID and reject unrelated targets or changed serving revisions. Treat
  the terminal service tier as the actual processing tier; permit automatic
  resolution earlier while rejecting conflicting concrete interim reports.
  [Compatible Responses contract](https://developers.openai.com/api/reference/resources/responses/methods/create).
- [x] Reproduce and repair supported Responses stream shapes with strict terminal
  consistency checks. Prove final text and reasoning stay separate, and preserve
  cleanup and cancellation ownership.
- [x] Close unit coverage, rejection controls and independent review; retain live
  route evidence separately from offline protocol checks.
- [ ] Human acceptance of the repaired route and useful failure diagnostics.

Converse binary redacted reasoning now uses the
[bounded private replay representation](implementation_plan_f4o4a.md); never
display, discard or coerce those bytes. A model returning non-JSON under
prompted JSON remains a schema failure and must not bypass local validation.

The Runtime Responses tier-resolution repair completed a bounded live Luna 6
stream through text and completion. Luna 5.6 has the same reported rejection
signature but still needs live confirmation of the repaired route. A short
stream proof does not establish application-level puzzle reliability.
