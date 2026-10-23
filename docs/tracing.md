# Local tracing and content controls

Run local supports optional tracing through the application-owned OTLP exporter.
Leave TRACE_MODE empty to disable it. Set TRACE_MODE=OTEL for an OTLP HTTP traces
endpoint, or TRACE_MODE=LangFuse for a LangFuse server. The normal configuration
review exposes the applicable settings and validates their values before startup.

## Destinations and dashboard login

TRACE_TARGET identifies the collector's complete traces URL for OTEL, or the
LangFuse server base URL. Conventional loopback targets select the existing
managed local infrastructure profiles. A custom or remote target starts no local
tracing stack. Remote HTTPS uses certificate verification and the configured
CLIENT_CA_FILE when supplied; credentials must never be embedded in URLs.

The local OTEL profile exposes a separate Jaeger browser interface on
JAEGER_VIEWER_PORT, default 16686. TRACE_VIEWER_URL supplies an optional explicit
OTEL dashboard address. A remote collector does not imply a browser address;
without a verified viewer, no dashboard tab opens.

For LangFuse, configure LANGFUSE_ADMIN_EMAIL and LANGFUSE_ADMIN_PASSWORD for Run
local dashboard sign-in. These dashboard credentials are distinct from the
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY ingestion credentials. New local
provisioning and browser login consume the same configured account. Changing
initialization values does not reset an account in an existing database; use the
installation's actual credentials. Dashboard account credentials appear as
literal text only in the interactive local status, never in terminal output,
home/status JSON, log events, trace payloads or inspection descriptors.

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
