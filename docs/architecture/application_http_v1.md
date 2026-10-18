# Application chat HTTP contract

This optional, text-only adapter serves the included Studio and other UI clients.
It translates browser values into the existing LLM provider contracts; provider
transport values and authentication sessions are not browser DTOs. Composition
must explicitly inject the backend. Without it, these application capabilities
return HTTP 501. This contract does not execute an application workflow.

## Admission and discovery

All implemented routes require a loopback peer, the exact configured browser
Origin, and the ephemeral `X-API-Agent-Session` header. Preflight accepts only the
route's actual method and headers. Responses are private and not cacheable.
On configured application routes, Host, admission and method checks precede
body reads and framework buffering. Interpret metadata through the framework's own request
parser so header aliases and missing fields cannot change admission. Count actual
received bytes incrementally and reject an oversized chunk immediately,
including when the sender leaves the body open.
The shared request deadline includes body acquisition and dispatch.
These controls do not implement production identity or cookie authentication;
other deployments supply their own authorized composition.

`GET /app/agent/models` returns `models`, `default` and `reasoning_default`.
Each model has `value` (the exact selectable identifier), `label`, `description`,
`features`, `version`, `family`, `max_output_tokens` and `context_length`.
Unknown version/family/limits are null. Optional `reasoning_efforts` preserves
unavailable metadata versus an explicitly empty list.

`default` identifies the exact offered model selected by backend policy. A UI
must not replace it with a newer family member merely because one is offered.
Preserve an existing valid explicit user selection. `reasoning_default` is a
configured effort or null: null means omit the request setting and defer to the
provider. Supported effort choices do not establish a default, and omission is
distinct from explicit `off`. A configured effort must be supported by the exact
policy default before catalog discovery advertises it. Catalog discovery performs
no inference. Unset UI model settings follow later backend policy changes without
becoming saved explicit choices; users can reset an explicit choice to policy.

## Chat values

`POST /app/agent/chat` and `POST /app/agent/chat/stream` accept JSON:

```json
{
  "session_id": "d3eeab66-51af-41c9-8c22-1df9cc1eeb3f",
  "model": "example-chat-v2",
  "messages": [{"role": "user", "content": "Reply with a short greeting."}]
}
```

Use a fresh canonical UUID for every operation, including retries; null asks the
server to allocate one. This identifies an operation, not persisted conversation
history. Supply the desired conversation messages with each request. Omitted
`model` selects the backend's exact default. Optional `reasoning_effort` accepts
`off`, `low`, `medium`, `high`; omission uses configured/provider defaults.
Empty or null effort is invalid. Optional `cache_policy` accepts `off`, `medium`,
`high`; omitted cache/output controls retain normal provider semantics.
`max_output_tokens`, when present, is a positive integer at most 4,096 in this
bounded text adapter, also subject to advertised model capabilities.

Bodies are limited to 131,072 bytes, with at most 64 role/content messages,
32,768 characters per message, and roles `system`, `user`, `assistant`.
Duplicate JSON keys, nonfinite numbers, unsupported fields and unsupported
model/generation settings fail before inference.

The JSON result contains `session_id`, `response_text`, `outcome`, `usage` and
available `actual_model`, `service_tier`, `serving_revision`, `billing`. Outcomes
are `completed`, `length`, `refused`; token usage and billing remain measured or
explicitly unknown. The selected identifier and actual serving identifier need
not be identical. Arbitrary backend exception details are never returned.

## Streaming and liveness

The stream uses named SSE events with JSON data:

1. `session`: operation `session_id`, `heartbeat_interval_ms` and
   `heartbeat_timeout_ms` (normally 10,000 and 30,000). Both are finite numeric
   milliseconds greater than zero and at most 2,147,483,647, with interval less
   than timeout. Fractional milliseconds retain their value. Clients schedule
   heartbeats from the admitted interval; invalid backend hints fail before
   inference rather than being advertised.
2. `token`: incremental `content` text. A completion-only callback emits one
   honest content chunk; an optional streaming callback supplies genuine deltas.
3. `response_complete`, then `result` (the JSON result above), then `done`.
   Completion/done data identify the same session. The result is authoritative.

`error` and `cancelled` are terminal alternatives, never successful completion.
The included UI retains optional named clarification/approval presentation;
this backend does not emit or execute those continuations. Tool, rich result-set
and workflow event capabilities require their own implementation.

Each frame has raw and escaped-JSON limits of 131,072 bytes. The entire stream
is bounded to 1 MiB and 8,192 frames. The backend also bounds provider text chunks
and buffered data. Malformed, oversized, disconnected or incomplete streams
must not become successful responses.

`POST /app/agent/chat/heartbeat` and `POST /app/agent/chat/cancel` take only
`{"session_id":"..."}` under the same admission. They return the identifier and
explicit status. Unknown/stale identifiers return `unknown` without inference.
Heartbeats keep a running operation live; they cannot extend its actual request
deadline or revive an expired lease. A terminal status may arrive while final
stream bytes are still in transit.

Stop and page exit should send best-effort cancel and abort the transport.
Visibility changes alone do not cancel. Disconnect handling and heartbeat expiry
cover missing page-exit notifications. Cancel is idempotent; acknowledgement of
completion waits for owned disposal. Pending cleanup retains a retry receipt and
blocks unsafe new admission. ASGI request ownership explicitly disposes the exact
stream source even when disconnect occurs while writing a yielded frame; iterator
finalization or garbage collection alone is insufficient. Response disposal targets the exact captured operation, so an old response cannot
cancel or wait for a newer operation that reuses its identifier after retention.
Ordinary cancellation does not close the persistent
authentication owner. Already incurred usage remains part of accounting.

State is bounded and in memory for one authorized backend owner. Requests and
controls must reach that same single-worker runtime; distributed recovery is not
provided. Result-set routes remain explicit HTTP 501 until implemented.

## Inspection boundary

This contract supports requests while its explicitly supplied provider owner is
valid. It does not keep pytest's finalized provider execution alive. Retained
post-test chat requires the separate [interactive ownership work](../prds/implementation_plan_i2c2.md),
with fresh deadlines and accounting separate from immutable published results.
The current Studio uses an absolute 150-second browser safety deadline; heartbeat
never renews that deadline. Full local workflow execution remains separately
scoped.
