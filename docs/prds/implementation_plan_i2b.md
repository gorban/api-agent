# Optional Studio completion bridge

[Studio frontend](implementation_plan_i2a.md)

The initial buffered provider-shaped HTTP proof below is historical. The current shared browser-facing DTO, named SSE and heartbeat/cancel requirements are owned by [I2c3](implementation_plan_i2c3.md), [I2c4](implementation_plan_i2c4.md) and [I2c5](implementation_plan_i2c5.md). The provider wire contract remains separate.

- [x] Composition explicitly authorizes and injects a caller-owned backend with
  a persistent LLM session owner, catalog/completion callbacks, model policy,
  exact loopback browser origin and ephemeral session nonce. No automatic
  identity selection, provider fallback or workflow execution is implied.
- [x] Preserve deferred HTTP behavior when no backend is configured. Admit
  configured requests only after loopback peer, exact origin and nonce checks;
  constrain preflight to the configured method and headers: catalog GET requires
  only the session header; completion POST requires content-type and session.
- [x] Bound JSON bodies, reject duplicate keys and nonfinite values, validate
  text contracts and actual catalog capabilities before inference. Catalog
  discovery may authenticate through the shared owner but performs no inference.
- [x] Use shared operation progress/deadlines and persistent session ownership;
  cancel and drain request work on disconnect/timeout and close ownership on
  application disposal. Retain contexts with pending nested cleanup and reject
  new work; expose disposal retry and retain authentication plus the runtime
  provider/session/tracing dependency tree until cleanup succeeds. Preserve
  request cancellation even when cleanup remains pending. Never expose arbitrary
  exception text or credentials.
- [x] Return existing result wire values, including reported metadata. The
  POST-SSE adapter buffers one complete result and emits a single completed
  event. Bound both the raw response bytes and escaped complete-event JSON to
  the included consumer limit; return a bounded error if framing cannot fit.
  This does not certify incremental provider streaming or graph execution.
- [x] Verify controlled HTTP admission, catalog/default policy, malformed input,
  completion, SSE, cancellation, safe errors and disposal with exact authored
  coverage and focused mutations before authorized external inference.

## Included host process

- [x] Active included UI composition supplies a default loopback Node process
  service and permits explicit service injection. Missing Node or resources
  produce actionable setup errors; optional caller-supplied access tokens cross
  only the process environment boundary.
- [x] Launch under shared progress/deadlines, use verified TLS and bounded local
  HTTP readiness, and require the exact per-launch ephemeral identity header.
  Enforce the clipped absolute startup budget before each probe and after every
  probe result or transport error, before admitting readiness or retrying. A
  consumed cancellation cannot extend startup, admit a late matching identity,
  or replace an expired deadline with a later child-exit outcome. Client context
  disposal must finish before a final clipped-budget check acknowledges success;
  a late client exit cannot consume expiry and transfer process ownership or
  replace the expired outcome with a transport error. Ordinary pre-deadline
  errors and genuine external cancellation retain their original classification,
  including an original external interruption followed by expiry during client
  disposal; preserve its cancellation arguments and owned cleanup duties.
  Process-level interruptions also retain their exact exception objects after
  expiry; only ordinary exceptions receive deadline normalization.
  Tighter parent deadlines remain authoritative. A foreign listener cannot satisfy readiness. Drain the owned direct host on
  launch failure or cancellation; retain cleanup retries and never signal a
  cached process-group identity. Preserve provider dependency ownership until
  cleanup confirms exit.


Controlled readiness proofs include deadline-consuming clients, late matching
and foreign responses, cancellation transformed into a transport error, an
expired client-entry scope and a tighter inherited deadline with exact retained
cleanup. Real loopback HTTP controls require an observed foreign identity before
cutoff and verify owned-child disposal while the foreign listener stays alive.
A bounded outer watchdog does not replace that observed-response ordering proof.
These controls establish the deadline invariant; they do not identify which
third-party transport consumed cancellation in any particular prior execution.
