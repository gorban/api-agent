# Explicit response modes

New `CompletionRequest.response` requests select `structured`, `json`,
`prompted_json` or `text`, with explicit `allow` or `reject` fallback. Existing
`CompletionRequest.structured` requests retain their original mapping, including
explicit `strict=False`, omitted properties and schema defaults. The two request
contracts cannot be combined. No mode changes authentication, selected model,
endpoint, API route, tools, reasoning, cache controls or generation limits.

Preflight can descend from structured to JSON mode and then prompted JSON, or
from JSON mode to prompted JSON. Explicit weaker choices never upgrade. Reject
fails before dispatch if the requested mode is unqualified. Inference failures,
invalid generated JSON and refusal/length outcomes never trigger a weaker replay.

## GPT capability scope

On the official API endpoint, the native schema/JSON matrix includes the exact
GPT-5.6 Luna/Terra/Sol, GPT-6 Luna/Sol/Astra and GPT-6.1 Sol aliases, plus
`gpt-4o-mini`, `gpt-4o-mini-2024-07-18` and `gpt-4o-2024-08-06`. The
[OpenAI structured-output guide](https://developers.openai.com/api/docs/guides/structured-outputs)
and individual [model contracts](https://developers.openai.com/api/docs/models/)
were checked October 27, 2026. Unlisted names, alternate endpoints and combined
native response-format/tool requests remain unqualified for the new policy.
Existing model/reasoning/tool restrictions still apply independently.

The existing qualified GPT-6 Luna subscription Responses schema route remains
supported on its official endpoint. That qualification does not establish native
JSON mode or other subscription model combinations. Permitted fallback uses the
same supported text route with JSON instructions; it makes no native enforcement
claim. No new live qualification is claimed for these policy mappings.

Native schema admission uses a conservative subset: object properties, required
keys, additionalProperties, arrays/items, scalar types, enum, title and description.
Strict requests require closed objects and all properties required. Unsupported
constraints, references or compositions cause rejection or a permitted weaker
mode; preflight never deletes constraints, adds defaults or closes an open
object. Non-strict native schema requests remain hints, not strict enforcement.

## Instructions, validation and evidence

JSON mode sets the API's `json_object` format and requires an explicitly object
root schema. Other JSON value roots require prompted JSON or reject when
fallback is disallowed. Both JSON mode and prompted JSON
append one separate user instruction containing the original schema, preserving
authored message roles and order. The public limit remains 64 authored messages;
adaptation adds at most one separately identified encoded instruction after
authored-request validation, within the existing 2 MiB request bound. Text mode
adds no schema or formatting. The canonical scrubbed capture records the effective provider body once per logical
call, including across existing pre-inference admission retries, with the
position/purpose of adapter-added instructions. Instruction paths are JSON
Pointers relative to the captured `provider_payload`, computed after wire
encoding and tool/continuation expansion. Schema/prompt text is absent from
bounded response metadata; canonical reporting payload content follows
`LOG_CONTENT`, while trace content follows `TRACELOOP_TRACE_CONTENT` independently. Content-free resolution and
dispatch metadata remain available with both flags off; credentials are always
scrubbed.

Preflight resolution alone never becomes terminal actual-mode evidence. Observed
transport admission records the chosen format, not proof of paid execution. Its
evidence survives deadlines, cancellation and operational failures through
the canonical call receipt; explicitly reported usage is retained, and absent
usage remains unknown. Cancellation and error types do not trigger replay.
Terminal response evidence records requested/actual modes, fallback policy,
controlled downgrade reason, exact original-schema SHA-256 and original strict
flag. Completed JSON in every JSON-producing mode must satisfy the original
schema locally. Tool calls, refusals and length outcomes retain their own
outcomes rather than being accepted as completed generated JSON.
