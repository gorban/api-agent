# HTTP trace diagnostics and safe request names

[Operation ownership](implementation_plan_k1a1.md)

## Requirements

- Default `TRACE_HTTP_DIAGNOSTICS_LEVEL=off` omits shipped GET health probes,
  POST heartbeat and genuine browser preflights before initializing request tracing.
  `info` restores them. Filtering preserves endpoint behavior and logging, and
  intentionally also omits failed responses on these admitted diagnostic routes.
- Trust actual built-in handler/backend and resolved view identity, not path
  spelling. Injected handlers, custom backends and unknown routes retain ordinary
  tracing. A classification failure falls back to method-only tracing.
- Name admitted built-in requests with a finite method and fixed route template.
  Unknown routes use method-only names; unknown methods use `HTTP`. Never export
  mount prefixes, query strings, arbitrary paths, headers or result identifiers.
- Export finite `http.request.method`, optional `http.route` and bounded
  `http.response.status_code` through mandatory credential scrubbing, independently
  of payload flags. Record status only after successful header delivery; it does
  not establish application outcome.
- Preserve existing non-HTTP operation naming and cancellation ownership. Request
  metadata is scoped to the exact owner and request and reset on all exits.

## Verification

- [x] Real serialized OTLP controls for both built-in tracing backends and all four
  log/trace payload combinations; strict config validation and sensitive-path rejection.
- [x] No-initialization controls for Application and direct Studio bridge, custom
  dispatch rejection, mounted/dynamic routes, preflight shape and cancellation.
- [x] Focused causal controls, independent review and complete unit coverage gate.
- [ ] Human confirmation of reduced diagnostic noise and useful request names.

Verification: 26,961 unit tests pass with exact 100% line and branch coverage.
The helper's 46 focused controls cover all statements and branches. Independent
adversarial and external source reviews pass, including real collector naming,
content-policy and lifecycle checks. Guard-removal mutations reject lost filtering,
unowned dispatch, unsafe metadata and premature status recording. Regenerated
diagram provenance retains identical diagram content.
