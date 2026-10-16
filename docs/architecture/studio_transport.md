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
