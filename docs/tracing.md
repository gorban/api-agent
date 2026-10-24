# Local tracing and content controls

Run local supports optional tracing through the application-owned OTLP exporter.
Leave TRACE_MODE empty to disable it. Set TRACE_MODE=OTEL for an OTLP HTTP traces
endpoint, or TRACE_MODE=LangFuse for a LangFuse server. The normal configuration
review exposes the applicable settings and validates their values before startup.

## Destinations and dashboard login

TRACE_TARGET identifies the collector's complete traces URL for OTEL, or the
LangFuse server base URL. Conventional loopback targets select the existing
managed local infrastructure profiles. Managed LangFuse recognizes exactly
`http://localhost:3000` and `http://127.0.0.1:3000`, each with zero or one trailing
slash. Other valid LangFuse spellings are custom and require explicit credentials.
A custom or remote target starts no local tracing stack. Remote HTTPS uses certificate verification and the configured
CLIENT_CA_FILE when supplied; credentials must never be embedded in URLs.

When changing TRACE_MODE, also choose the matching TRACE_TARGET default in Review
configuration. A previously saved target remains explicit: local LangFuse uses
`http://localhost:3000`, while managed local OTEL uses
`http://localhost:4318/v1/traces`. The application does not silently replace a
saved custom collector.

The local OTEL profile exposes a separate Jaeger browser interface on
JAEGER_VIEWER_PORT, default 16686. TRACE_VIEWER_URL supplies an optional explicit
OTEL dashboard address. Omitted and explicitly empty values both select the
managed local viewer on JAEGER_VIEWER_PORT when TRACE_TARGET is the conventional
loopback OTLP traces endpoint (port 4318, path /v1/traces). A custom collector,
including a loopback target on another port, does not imply a browser address or
start local infrastructure. Set TRACE_VIEWER_URL explicitly for that collector.
Without a verified viewer, no dashboard tab opens and startup logs an actionable
fixed diagnostic. Readiness requires HTTP 200 without redirects; a failed probe
keeps the viewer unavailable. The collector target is preserved.

For LangFuse, configure LANGFUSE_ADMIN_EMAIL and LANGFUSE_ADMIN_PASSWORD for Run
local dashboard sign-in. These dashboard credentials are distinct from the
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY ingestion credentials. New local
provisioning and browser login consume the same configured account. Changing
initialization values does not reset an account in an existing database; use the
installation's actual credentials. Dashboard account credentials appear as
literal text only in NO_STD_IN terminal status on stderr; the interactive TUI
omits them. Redirecting stderr captures that literal status. Structured stdout,
home/status JSON, log events, trace payloads and inspection descriptors exclude
them independently of content settings.

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
TRACELOOP_TRACE_CONTENT does not enable log or Allure content. Credentials,
authentication/continuation state and hidden reasoning remain excluded under all
combinations. Timing, status and admitted usage metadata remain useful without
payloads. Enabling content permission does not itself enable a tracing backend.

The current join emits owned manual graph/component/node/client spans using
shared workflow identities. It captures only declared supported plain user I/O;
unsupported or oversized payloads remain explicitly unavailable. Automatic HTTP
instrumentation, broader metrics/propagation conformance and deployment-specific
AI dashboard acceptance retain their separate requirements. A successful local
trace is not evidence of deployed backend equivalence.

Manual trace model labels require exact identities established by their provider
catalog or an explicit owner-controlled binding; identifier syntax alone is insufficient. Unclassified selection text is omitted
from model attributes and uses a fixed chat span name. This affects telemetry
projection only; provider requests, results and failed-call accounting stay exact.

Custom recorded-workflow owners must supply resolved content authority through
the observation factory or the engine's `content_policy` service (`ContentPolicy`).
Raw configuration Booleans and credential scrubbers alone do not authorize
recorded payloads; missing authority withholds them. Explicit denial cannot be
raised by later adapter configuration.

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
