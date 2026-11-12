# Local tracing and content controls

The bundled default `TRACE_PROVIDER` contributes `TRACE_MODE` and its nested
settings through ordinary configuration discovery. Leave `TRACE_MODE` empty to
disable tracing without allocating an exporter, dashboard or collector. Select
`OTEL` or `LangFuse` to reveal that route's settings in Review configuration.
A custom provider replaces the bundled settings with its own declarations.
Help and Review read declarations without executing provider factories.

For extension boundaries, see [tracing providers](tracing-providers.md).

## Destinations and dashboard login

`TRACE_EXPORT_ENDPOINT` is always the complete trace ingestion URL:

| Bundled mode | Default ingestion URL |
| --- | --- |
| LangFuse | `http://localhost:3000/api/public/otel/v1/traces` |
| OTEL | `http://localhost:4318/v1/traces` |

Adapters use that URL unchanged. CLI, environment, saved and interactive settings
follow normal configuration precedence. An explicit empty endpoint stays empty
and is rejected when active tracing needs a usable destination. Changing modes
does not replace an explicitly saved endpoint; update or clear it in configuration.
Remote HTTPS verifies certificates and uses `CLIENT_CA_FILE` when supplied.
Credentials must never be embedded in URLs.

`TRACE_VIEWER_URL` is the separate dashboard address. Omitted viewer settings
can derive the managed local dashboard for an exact conventional local ingestion
URL: LangFuse on port 3000, or Jaeger on `JAEGER_VIEWER_PORT` (default 16686).
An explicit empty viewer disables dashboard presentation. Remote/custom ingestion
requires an explicit viewer URL if a dashboard is wanted; the application does
not infer a remote dashboard from an ingestion path. A remote/custom ingestion
or dashboard does not start a local collector stack. Readiness and authenticated
dashboard identity remain separately verified.

Only HTTP/protobuf export is supported. Review offers that protocol or an empty
setting that permits the supported OTEL environment default. Explicit gRPC or
HTTP/JSON requests fail actionably before exporter allocation.

The OTEL route reveals `TRACE_METRICS` as an opt-in for a separate supported
metrics destination. Managed Jaeger's trace ingestion does not accept metrics.
After opt-in, configure the complete `TRACE_METRICS_ENDPOINT` or the explicit
signal-specific `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`; the bundled provider does
not derive metrics from a general OTLP base. Temporality and metric vocabulary
settings appear only after opt-in. LangFuse remains trace-only and hides these
controls. Saved inactive fields from the selected provider remain removable
through Clear `.env`.

Inactive saved, environment or CLI settings follow ordinary configuration
applicability and remain inert. Capability rejection applies to active admitted
requests; a hidden metrics value does not enable an unsupported signal.

For LangFuse, configure LANGFUSE_ADMIN_EMAIL and LANGFUSE_ADMIN_PASSWORD for Run
local dashboard sign-in. These dashboard credentials are distinct from the
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY ingestion credentials. New local
provisioning and browser login consume the same configured account. Changing
initialization values does not reset an account in an existing database; use the
installation's actual credentials. Applicable dashboard account credentials appear
as literal text in the interactive Run local summary and in NO_STD_IN terminal
status on stderr. The summary scrolls independently above the logs; use Tab to
move between panels. Redirecting stderr captures that literal terminal status. Structured stdout,
home/status JSON, log events, trace payloads and inspection descriptors exclude
them independently of content settings.

Changing `LANGFUSE_ADMIN_EMAIL` or `LANGFUSE_ADMIN_PASSWORD` can make a retained
managed web container incompatible with the new configuration. The recovery
notice names changed initialization settings when inspection proves them,
without showing credential values. Otherwise it explains the possible cause
without asserting which setting changed.

To preserve traces, stop every Run local, trace viewer and test invocation using
that stack. In the same container engine/context, confirm the identified web
container is stopped and remove only that container using the notice's command.
Keep all volumes; do not force removal or use `-v`, `compose down -v`, or volume
pruning. Rerunning recreates the web container. Existing database accounts keep
their actual credentials; recreating a container does not rotate the account.
Shared test setup retains this recovery notice in successful-test stderr details
when tracing is unavailable, while the original functional request continues.

When RUN_UI or DEBUG opens the headed startup browser, tabs appear as home,
ready tracing dashboard, actual selected workflow, and the optional prepared UI
last. Without either flag there is no automatic browser or dashboard sign-in.
Backend/viewer readiness and authenticated dashboard state are separate. Startup
tabs share their browser context and cookie jar; ports do not isolate cookies.
Application-owned cookie names must avoid collisions, with HttpOnly/Secure
preferred where supported. The launcher does not rewrite dashboard cookies.

## Independent content permissions

Both flags default False and require an explicit valid True to permit content:

| Setting | Controls |
| --- | --- |
| LOG_CONTENT | Application diagnostics, local logs and Allure details/screenshots |
| TRACELOOP_TRACE_CONTENT | Permitted payloads in both OTEL and LangFuse spans |

Either can be enabled independently. LOG_CONTENT does not enable trace payloads;
TRACELOOP_TRACE_CONTENT does not enable log or Allure content. Provider-returned
plaintext reasoning and summaries are optional content, separate from the final
answer. Credentials, encrypted reasoning, redacted thinking and internal
authentication/continuation state remain excluded under all combinations.
Omission notices name the destination's flag and require a new run; unavailable
provider text cannot be recovered by enabling capture. Timing, status and admitted usage metadata remain useful without
payloads. Enabling content permission does not itself enable a tracing backend.

`register_secret` keeps a credential scrubbed for the life of the emission
boundary, which suits configured values. A service that receives rotating
credentials (per-user tokens, refreshed sessions) takes a `credential_owner()`
from the boundary for each credential owner, such as a user session, and
registers through it. `register(*values, slot=...)` replaces what that slot
registered before, so a rotation retires the previous token. Wrap any
operation that can still emit a token in `owner.hold()`: the held token stays
scrubbed until the hold is released, even after it is superseded or the owner
is closed. `owner.retire(slot)` ends one slot when its credential has no
further use, and `owner.close()` ends all the owner's slots when its session ends.
Removal waits for open evidence leases, and a value that is also registered
unscoped or by another owner stays scrubbed. Registrations made through an
owner that is never closed stay for the life of the boundary.

`boundary.call_credentials()` is the owner every operation of a boundary shares
for the credential it authenticates with: each operation holds it, registers
its credential in the `call` slot and releases the hold when it ends, so the
registry keeps only the credentials of operations still running.

Password-manager one-time codes expire, so a reader bound to the boundary
registers them for one sign-in instead of for the process. A sign-in runs
inside `credentials.sign_in()` (or `signing_in(credentials)` for any reader):
the scope holds a credential owner, the codes the sign-in reads register in
it, and it closes when the last overlapping sign-in ends. A code read outside a
sign-in is refused. Usernames, passwords and login material stay registered
for the life of the boundary.

Shared-provider integration tests use a trace runtime owned by their shared
backend. Its `chat` spans and child `transport attempt` spans carry the same
logical-call and physical-attempt IDs as accounting and Allure. These remote
spans are correlated by ID, not attached to a cross-process workflow parent.
Request/response Events include the capture phase, source and reason. Explicit
trace permission survives log opt-out; trace-only content is not written into
pytest evidence streams, journals or Allure. Before SDK queuing, the backend
projects against the complete provider/operation/caller credential union and
rechecks its generation. Each destination has a separate bounded collector;
trace projection clips strings at 65,536 characters within its admission budget.
Exporter shutdown is owned by backend cleanup, before result publication.
Normalized-only streaming adapters still cannot supply discarded SDK reasoning.

The current join emits owned manual graph/component/node/client spans using
shared workflow identities. Both parsed-flow executors capture schema-admitted
JSON inputs and outputs automatically, without application-specific wrappers.
Stateful declarations use their admitted state annotations. Explicit restrictive
capture contracts remain respected. Unsupported or oversized payloads carry
unavailable, partial or truncated evidence. Automatic HTTP
instrumentation, broader metrics/propagation conformance and deployment-specific
AI dashboard acceptance retain their separate requirements. A successful local
trace is not evidence of deployed backend equivalence.

In Jaeger, select a node span and expand **Events** to find
`api_agent.event` entries. Their `direction` is input or output; `values` contains
the admitted node fields. In-process shared provider calls also capture visible messages,
tool/format contracts and completions as `llm_request` and `llm_response` values.
Their shared `cause_id` pairs capture observations within the owning node visit;
it does not create another accounting call or charge. Capture depth, collection
counts and aggregate text are bounded; retained journal strings have a
512-character preview and live/trace capture permits up to 65,536 characters
within its aggregate admission budget. Credentials, continuation and hidden
reasoning carriers are removed before schema filtering or display clipping.
The separate `chat` span carries admitted model/usage metadata, native JSON
panels and normalized GenAI messages where the source form is supported. Native
panels and normalized messages are separate projections; neither invents missing
provider output. Broader conformance remains under the
[tracing schema and AI-view work](prds/implementation_plan_k1a.md).
Restart the application to use changed capture code. Previously withheld traces
and Allure artifacts cannot be populated retroactively.

The integration-test backend uses its authenticated trace ownership and evidence
channels. Remote parenting requires an admitted explicit parent; unrelated calls
cannot adopt ambient workflow spans. Broader distributed propagation retains the
[worker/provider propagation gate](prds/implementation_plan_k1b.md). Reporting
evidence cannot authorize trace-only payloads or replace the credential boundary.

Manual trace model labels require exact identities established by their provider
catalog or an explicit owner-controlled binding; identifier syntax alone is insufficient. Unclassified selection text is omitted
from model attributes and uses a fixed chat span name. This affects telemetry
projection only; provider requests, results and failed-call accounting stay exact.

Custom recorded-workflow owners must supply resolved content authority through
the observation factory or the engine's `content_policy` service (`ContentPolicy`).
Raw configuration Booleans and credential scrubbers alone do not authorize
recorded payloads; missing authority withholds them. Explicit denial cannot be
raised by later adapter configuration.

## Rejected output and final failures

A failed generation's native output can include `application_failure` alongside
the original response. With trace content enabled, it contains bounded known
provider details or visible rejected candidate text and safe validation facts.
For example, a length rejection can identify `maxLength`, its limit and the
actual character count. Encrypted continuation is excluded. Unknown exceptions
contribute fixed classifications rather than arbitrary exception text or stacks.
Validation facts come from the originating schema's validator, including when
validation runs inside an adapter. Provider-supplied claims cannot establish them.
Counts describe the original checked candidate, before credential redaction.
Known model, tier and revision metadata can survive local output rejection;
unavailable or ambiguous source metadata is omitted.
If no response was received, failure-only evidence does not invent an assistant
message. Existing native keys and unavailable/withheld dispositions are preserved;
an annotation that cannot fit does not erase the original response.

Fixed failure cause/code metadata remains useful without content permission.
Trusted provider adapters can also publish finite, allowlisted failure facts:
HTTP status, provider type/code, policy recommendation/categories, and explicit
retry attempt/limit. These structural fields never include provider messages,
response bodies, prompts, or stacks, regardless of content permission. Unknown
provider labels are omitted. `WorkflowObservations.export_trace_context()` retains
the explicit graph span's validated trace/span IDs after closure so local evidence
can link to the exported trace without depending on ambient OTEL state.
When a trusted boundary has one canonical failed-call identity,
`api_agent.outcome.cause_id` links affected node and terminal graph/request spans
to that call. Missing or ambiguous identity stays unavailable. Application
validation can fail after a physical transport completed successfully; those are
different outcomes. A repaired candidate leaves a failed call/node visible while
the completed graph and request remain successful. A handled nested graph failure
also does not make a successful outer request fail.

See [candidate repair](workflow_repair.md) for retry limits and a controlled
length-rejection test. Restart to use changed tracing code: missing details in
older traces cannot be reconstructed retroactively.

## UI-source activity

The UI source reports bounded child-process output activity; arbitrary streamed
stdout/stderr content remains withheld. The included Studio host normally emits
no process output for browser clicks, so an empty UI source is expected. Opening
Settings retrieves model metadata and sending a message invokes the agent API;
those catalog/request diagnostics belong to the agent source.

Stop, confirmed Quit and terminal Ctrl+C wait for owned cleanup. Each cleanup
attempt has a 15-second budget. If work remains, Run local shows cleanup pending
and retries while keeping its event loop alive. This bounds each attempt, not
the time until process exit; repeated Ctrl+C does not abandon pending cleanup.

## HTTP request diagnostics

`TRACE_HTTP_DIAGNOSTICS_LEVEL` defaults to `off`. Shipped GET health probes,
POST heartbeat requests and genuine browser preflights on built-in routes are
omitted before request tracing initializes. This also omits failed responses on
those diagnostic routes; endpoint behavior and logging are unchanged. Choose
`info` to trace them. Ordinary OPTIONS requests and custom handlers remain traced.

Built-in request names use a method and fixed route template; unknown/custom
routes use only the method, with `HTTP` for unknown methods. Structural method,
route and delivered response status remain credential-scrubbed regardless of
payload settings. Paths, queries, mount prefixes and dynamic result identifiers
are never request names. HTTP status does not determine application outcome.

Stateful native boundaries can supply `node_io_projections`, a services mapping
from node ID to a synchronous `projection(direction, state)` callable, and
`graph_io_projection`, a synchronous `projection(direction)` callable. Each
returns a JSON dictionary containing only the boundary's admitted input or
terminal summary. Projection runs before capture admission so large internal
records do not consume the summary's budget. Returned dictionaries still pass
through the normal credential authority, independent log/trace permissions,
schema projection and capture limits. These hooks do not change workflow state
or authorize content export. Keep projection callbacks bounded and free of I/O.
A projection exception or invalid return emits a content-safe warning and an
unavailable capture receipt; it does not fail application execution.

### Explicit returned outcomes in stateful workflows

A trusted stateful-workflow owner may provide the synchronous service callback
`scope_outcome(kind, owner, state)`. `kind` is `node` or `component`; `owner` is
the declared identifier. A node receives its prior state merged with its actual
returned update, and a component receives the state at its declared END exit.
Return an `ExecutionOutcome` to report success, failure, cancellation, partial or
unknown status for that scope, or `None` to keep the normal passed disposition.
Any other return value is a configuration error. The generic runtime does not
inspect product response fields or infer outcomes from their content.

This callback reports an outcome independently of routing: returning a failure
outcome does not throw an exception, cancel component ownership or prevent a
normal subsequent edge. Raised exceptions, deadline expiry and task cancellation
retain their existing propagation and draining behavior. Root
`execution_outcome()` remains the separate final graph contract. Journal route
validation accepts a non-passed semantic scope only when its scope end matches
a node's recorded return disposition, or a component's normal-finish
disposition and completed internal END route. Legacy return events without a
disposition imply success. A changed or aborted scope cannot manufacture a
successful continuation. Outcome reporting does not
change trace, log, capture or independent privacy preferences.
