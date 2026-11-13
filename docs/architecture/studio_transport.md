# Studio browser adapter

The browser request mirrors `providers.inference.request_to_wire`'s bounded text
request: `selected_llm`, `messages` (`role`, `text`), nullable
`max_output_tokens` and `cache_policy`, and `request_id`. Optional
`reasoning_effort` preserves omission versus explicit `off`. Each request uses a
unique `request_id`; it is not a server conversation identifier. A selection's version
is catalog metadata, not an additional request field. The composer checks the
selection and its advertised capabilities before sending.

Catalog HTTP JSON wraps the array produced by `providers.catalog.catalog_to_wire`
as `{models: [...], default: selected_llm_or_null}`, using
`AvailableSelection`'s exact `selected_llm`, `display_name`, `version`, `family`,
`features` and optional `reasoning_efforts`. Unknown effort capability does not
permit explicit effort. An omitted model resolves only through the validated explicit
server default; it never selects an arbitrary catalog row. The normalized array
retains that choice as nonenumerable `defaultSelection`. The adapter never invents
model/version/reasoning metadata.

Successful JSON responses use `providers.inference.result_to_wire`'s text result.
Only its documented fields are accepted; richer payloads and unknown fields fail
before terminal callbacks. Optional billing uses the exact bounded
`usd`/`request_id`/`generation_id` evidence schema.
Authoritative terminal text replaces accumulated stream text. Provider-reported
`actual_model`, `service_tier` and `serving_revision` remain separate observations.

Optional `reasoning_summary` is bounded displayable provider summary text, separate
from the final answer. It is not an opaque continuation or raw thinking carrier.
The named application SSE stream emits `thinking` with `{}` and may emit
`reasoning_token` with `{content: string}` for progressive summaries. Existing
`token`, `response_complete`, `result` and `done` events retain their roles. A
terminal result's summary is authoritative; progressive previews are transient
and cannot become persisted final details after a missing/failed terminal.

Studio renders summaries as plain text, shows a pending animated indicator,
replaces the preview with the final answer, and keeps the completed summary in
collapsed response details. Reduced-motion preferences disable animation. Browser
test helpers wait for terminal completion, final DOM content and removal of the
pending indicator before assertions and per-turn screenshot capture.

Incremental summary delivery depends on the selected provider/operation. Buffered
workflow calls can publish an available summary after each node call; this does
not imply native per-token streaming or fabricate summaries when none are supplied.

### Live call and tool progress

An application may supply `StudioBackend(progress_projector=...)`, an async
callable taking a `CallProgress` (`node`, `attempt`, `request_id`, `response_text`,
`reasoning_summary`, `tool_calls`) and returning display text or `None`. For
`chat_stream` only, Studio binds `providers.presentation.call_progress` and
`tool_progress`; the workflow engine then reports each completed model call
(after any display-summary projection) with its step id, and each tool dispatch
with its name. Studio streams non-empty projected text as
`reasoning_token {content, section}`, where `section` is `node:attempt` (or the
node id when no attempt is known), and tool lifecycle as `tool_start {tool}` and
`tool_end {tool}`, in workflow order before `token` and `result`.

Projection runs in one task per turn so it never delays the workflow; the turn
waits for it inside the existing Studio completion budget. A projector failure or
non-text result skips that update and reports only a fixed classification to
`on_error`; it never fails the turn. Output is bounded to 4096 frames and 64 KiB
of text per turn; excess frames are dropped. While a projector is bound, raw
provider summaries are not streamed live, because the projection owns reasoning
display. Without a projector, frames are unchanged.

The following POST-SSE envelope is a **browser adapter contract**, independently
specified for controlled tests and the opt-in completion bridge:

- `data: {"kind":"text","payload":{"text":"fragment"}}`
- `data: {"kind":"completed","payload":<text result>}`
- `data: {"kind":"error","message":"safe failure description"}`
- `data: {"kind":"clarification","payload":{"id":"q","question":"…","options":["…"]}}`
- `data: {"kind":"approval","payload":{"id":"q","question":"…","options":[{"id":"allow","label":"Allow"}]}}`

Questions terminate this response and retain their actual structured content.
There is no implemented structured continuation request contract; submitting one
is explicitly unsupported. Heartbeat frames require an explicitly advertised
capability passed as `heartbeat: true`; they are never assumed. UTF-8, CR/LF and
frame boundaries may cross chunks. Requests and responses have a 131072-byte
limit. Semantic JSON limits match Python compact ASCII-escaped serialization,
with code-point string lengths; actual received HTTP bytes are independently
bounded before parsing or decoding, including catalog JSON. Shared reader cleanup
cancels and releases the stream on every exit. Missing terminals, frames after a terminal, malformed input, cancellation
and HTTP errors remain errors, including partial streams.

The optional bridge emits one buffered terminal frame; it does not claim
incremental provider streaming or workflow execution. Its exact-origin loopback
requests require an in-memory session header supplied through host configuration.
Adapter tests establish browser behavior; the separate real-listener browser
request test verifies the selected provider join. HTTP errors, including deferred 501 responses, are
never converted into successful replies.
