# Tracing providers

[Resource packs](resource-packs.md) · [Contract and examples](architecture/tracing_provider_contract_v1.md)

A pack can select a tracing backend with `TRACE_PROVIDER` using the same
owner-relative `file.py:create_tracing_provider` convention as other providers.
Its adjacent `cfg/config.toml` contributes ordinary typed, nested settings.
Help, review and collection read declarations without executing factories.
The bundled default provider contributes `TRACE_MODE`: blank disables tracing,
while OTEL and LangFuse select declarative capability profiles. Selecting a custom
provider replaces those bundled controls. `LOG_CONTENT` remains independent from
the shared trace-content permission. Review reveals only applicable provider
settings; Clear `.env` retains saved inactive fields in the selected declaration.

Export capabilities and dashboards are separate. A remote exporter need not
start any local services or provide a browser UI. A dashboard need not automate
login. When local services are declared, the shared infrastructure planner admits
only the selected recursive closure; existing readiness, leases and retained
volumes remain authoritative. Exporters flush and dispose before their final
collector lease is released. The shared headed viewer remains alive until Ctrl+C,
even if its browser window closes.

A provider declares its supported signals before allocation. `TRACE_METRICS=True`
requires metric support and a metric destination; the bundled OTEL profile requires
a separate explicit metric endpoint and never infers one from Jaeger or a general
OTLP base. Its metric endpoint, temporality and vocabulary controls appear only
after opt-in. LangFuse hides metrics controls. Trace-only backends receive no
inferred metric traffic. Current and legacy metric vocabularies are explicit
profiles. Shared capture supplies sanitized graph/node/tool/model observations
and known physical-attempt usage; a provider does not install another recorder.
Trace content follows `TRACELOOP_TRACE_CONTENT`, independently of `LOG_CONTENT`.
Credentials and hidden reasoning remain excluded from trace and metric data.
Metrics deliver on explicit owned SDK flush and runtime/session close. They do not
yet provide per-turn or periodic live updates; recording a call makes no HTTP
request. Trace batch export remains unchanged.

For a selected dashboard, use `./launch.sh --trace-viewer`. Only explicitly
named human-dashboard login fields can appear on that command's console.
Exporter credentials are never chosen as dashboard login values. Remote-only and
export-only operation need no managed collector. Dashboard readiness, optional
login preparation and same-origin identity validation use the existing viewer.

See the [version-1 contract](architecture/tracing_provider_contract_v1.md) for the
manifest, factory, signal settings, compatibility limits and synthetic example.
Current qualification and remaining gates live in the [provider PRD](prds/implementation_plan_k1a2.md).

## Backend presentation

Jaeger shows the request, graph, components, nodes, logical LLM calls, physical
attempts and named tools through their parent links. Its GenAI conversation panel
uses normalized model messages. In the tested Jaeger viewer, captured tool input and
output remain available as observation attributes but are not rendered in that
native tool panel. LangFuse renders the admitted LLM and tool input/output in its
native panels when trace content is enabled.

LangFuse may classify a physical transport attempt as a generation alongside its
logical LLM call. Its generation count therefore need not equal the logical call
count. Standard token usage belongs to the logical generation, aggregated once
from known physical attempts; physical observations retain separate transport
usage. Missing executed-attempt usage remains unavailable.

Timestamps retain their source precision. Physical attempts use millisecond
ledger times, while logical journal starts are reconstructed from elapsed time.
A physical start can appear slightly before its parent within the same
recorded millisecond bin; this does not establish an actual execution reversal.
Out-of-bin ordering or end-time reversals remain errors. Parent links,
attempt indexes and terminal states remain authoritative. Parallel branches do
not imply a total order. Broader signal and backend-view qualification remains
tracked in the [GenAI conformance requirements](prds/implementation_plan_k1a.md).

### Unavailable managed dashboard

The viewer reports the full selected `TRACE_PROVIDER`, profile selector and actual safe ingestion endpoint alongside the provider’s managed endpoint defaults. Changing a mode does not replace an explicitly saved endpoint: a saved endpoint from the previous mode can therefore disable the new mode’s managed dashboard. Remove the saved ingestion override to accept the selected profile’s default, or retain remote ingestion and set an explicit viewer URL. An explicitly empty viewer URL disables the dashboard; remove that override to restore managed derivation. URLs containing credentials, queries or fragments are redacted in this diagnostic.
