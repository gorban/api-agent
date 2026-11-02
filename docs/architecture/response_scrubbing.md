# Schema-aware response scrubbing

Providers use `api_agent.providers.scrubbed_response.scrub_response` for bounded functional JSON responses. Pass a trusted JSON Schema or an existing contract validator, the operation's frozen credential snapshot, and a fixed operation label. The result is `(clean_json, report)`.

The boundary validates both the original and cleaned document. It preserves JSON numbers, booleans and nulls; numeric-looking strings stay strings. Credential-bearing string values become empty strings. A required identity with a positive minimum length therefore fails at this boundary. Ordinary safe siblings and permitted additional properties survive. Duplicate JSON members, non-finite numbers, unsupported native values and exceeded budgets reject safely. `decode_json` provides the same bounded, duplicate-aware decoding when authentication or integrity requires exact values.

Callable validators receive independent JSON snapshots and cannot replace or mutate the returned document. Convert validated JSON into application objects afterward. Providers must guard any new values introduced by that conversion before publication. Never pass untrusted schemas into this in-process validator: model-supplied schemas retain the existing isolated schema-validation owner.

Credential-bearing member names are omitted from the ordinary object. Their scrubbed values remain in `report.anonymous_members`, an out-of-band sequence without the original name. No reserved JSON property or Symbol-like key is invented. The report's representation excludes those values; callers must not log or serialize the sidecar as diagnostics. More than 100 anonymous members rejects instead of silently losing values. Diagnostics are limited to 100 events with an explicit omitted-event count.

`SCRUB_DIAGNOSTICS_LEVEL=info` reports fixed operation labels and changed-field counts on stderr. `debug` adds safe schema paths and actions when available. Isolated SDK catalog workers forward bounded action counts rather than raw paths or child stderr; undeclared member names use numeric positions. Neither level prints values, matched credentials or anonymous members. The default is `warning`; `off` disables these diagnostic records. This setting does not enable LLM payload logs or tracing.

## Exact and evidence boundaries

`guard_json` and its asynchronous wrapper delegate to the same boundary in exact mode: a functional payload is preserved or rejected. Requests, signed identifiers, opaque continuation state and authenticated control frames must not be rewritten. Authentication owners deliberately acquire and transfer credentials through their private channels. Stream assemblers retain bounded lookbehind and final-carrier validation so credentials split across chunks cannot escape.

Evidence export remains separate. Its shared content policy enforces `LOG_CONTENT` and `TRACELOOP_TRACE_CONTENT`, excludes encrypted/internal state, and applies destination-specific limits. Functional response parsing must not reuse those evidence omissions to repair invalid data. Artifact signatures, digests and ownership are verified before publishing a scrubbed projection.

Use the existing authority generation check or hold its lease through validation and publication. A snapshot alone does not prevent another concurrent operation registering a new credential before the value is released.

## Structured test diagnostics

Known test-profile stdout records remain typed pytest properties through worker
capture. The parent supplies their trusted profile schema to this same boundary
with the final operation credential authority, then renders the clean JSON.
Credential-bearing values may become empty while safe sibling settings and
native types remain readable. Required-field validation still applies; unusable
diagnostic records receive an explicit withheld marker without changing the
functional test outcome. The rendered record and combined stdout also undergo
registered-credential checks. Repeated finalization reprojects the raw record
against newly registered credentials. Free-form stdout/stderr retains its
existing text protection; arbitrary printed JSON does not acquire a trusted
schema or bypass that protection.
