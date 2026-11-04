# Tracing-provider contract version 1

[Guide](../tracing-providers.md) · [Requirements](../prds/implementation_plan_k1a2a.md)

## Declare capabilities

A selected provider contains `provider.py`, `tracing-provider.toml` and optional
`cfg/config.toml`, `provider-dependencies.toml`, `infrastructure.toml` and notices.
For example:

```toml
version = 1
id = "example-tracer"
signals = ["traces", "metrics"]
input_attribute = "input.value"
output_attribute = "output.value"
endpoint_field = "EXAMPLE_TRACE_ENDPOINT"
secret_fields = ["EXAMPLE_EXPORT_TOKEN"]
metrics_from_base = false
```

`signals` admits traces, metrics or both. An empty list is valid only with a
separate dashboard capability. Native input/output mappings accept the shared
LangFuse or OpenInference attribute pairs; mapping never grants content authority.
Unsupported versions, shapes and capabilities fail before exporter allocation.
Descriptors, resolved values, adjacent configuration and executable closure are
captured. Later source-file changes cannot replace the admitted factory bytes.
The content-free binding report contains identities and capability states only.

## Declarative profile selection

Static version-1 descriptors remain supported. A version-2 descriptor can declare
one typed selector, an exact disabled value and a bounded list of profiles:

```toml
version = 2
id = "example-profiled-tracer"
selector = "EXAMPLE_TRACE_MODE"
disabled_value = ""

[[profiles]]
value = "remote"
signals = ["traces"]
input_attribute = "input.value"
output_attribute = "output.value"
endpoint_field = "EXAMPLE_TRACE_ENDPOINT"
```

Each profile carries the ordinary version-1 capability fields. Values must be
unique and disjoint from the disabled value; unknown selectors and malformed
profiles fail before allocation. The selector must be declared and available in
effective configuration; a missing or inactive field is an actionable admission
error. Only a present selector matching the disabled value returns no active provider.
Live resolution and protected restoration select from the same admitted bytes;
profile and effective destinations remain part of the binding identity.

The bundled default uses `TRACE_MODE` profiles for OTEL and LangFuse. Its adjacent
`cfg/config.toml` owns mode, endpoint, exporter, metric and dashboard controls.
Ordinary declaration predicates hide inactive settings in Review without backend
branches in the UI. Custom selectors contribute their own settings and do not
inherit those bundled controls. Shared content permission remains independent
from `LOG_CONTENT`. Help, Review and collection never import a factory or allocate
an SDK, network client, browser or collector.

The bundled managed profiles require exact admitted local ingestion and matching
dashboard settings. An omitted dashboard can derive the local default; an
explicit empty dashboard remains absent. A configured Jaeger viewer port changes
only that local dashboard port. Remote/custom ingestion or dashboards cannot
acquire the bundled local stack. Existing infrastructure leases and retained
volumes remain authoritative.

A dashboard is optional:

```toml
[dashboard]
url_field = "EXAMPLE_DASHBOARD_URL"
readiness_field = "EXAMPLE_DASHBOARD_READY_URL"
login = "none"
```

The URLs must be admitted HTTP(S) addresses at the same origin, without embedded
credentials, queries or fragments. Readiness does not imply authenticated access.
Optional `config_fields` names up to 32 unique declared configuration fields
always needed for dashboard preparation. Optional `managed_config_fields` uses
the same bounds for provisioning/readiness dependencies needed only when the
selected destination admits managed local infrastructure. For example, the
bundled LangFuse profile names its project keys there, preserving local collector
identity while remote dashboard login needs only its human-login credentials.
Neither list grants credential display permission.

The viewer resolves the selector and unconditional dashboard dependencies strictly.
It evaluates auxiliary ingestion and ownership controllers separately, without
export-only requirements. Missing or invalid auxiliary inputs cannot authorize
local infrastructure; an explicitly configured remote dashboard remains usable.
Available valid inputs retain ordinary defaults, applicability and precedence.
Auxiliary-only controllers do not become dashboard requirements through default
dependencies. Explicitly named dashboard inputs retain their strict requirements.
Managed dependencies resolve strictly before allocation, followed by a recheck of
the admitted descriptor/profile, ingestion/dashboard destinations and managed
closure. Ordinary tracing export retains its full configuration validation.
For automated preparation, use `login="provider"` and explicitly declare
`username_field` and `password_field`. These human-login fields cannot also be
exporter secret fields. The adapter supplies async `prepare_dashboard(page, *,
url, username, password)` and `dashboard_identity(page, *, url)`. The latter
reads authenticated state and returns the verified selected username. The shared
viewer enforces the origin and identity, and owns browser/CDP lifetime. There is
no additional human confirmation prompt.

## Factory and lifetime

The direct synchronous factory is:

```python
def create_tracing_provider(config, services):
    return Adapter(config, services)
```

The version-1 adapter has synchronous `create_exporter(plan)` and async `aclose()`.
The frozen `ExportPlan` supplies admitted trace/metric URLs, per-signal headers,
protocol, temporality and one resource for both SDK providers. Its representation
omits endpoints, headers and resource values. Return `ExportHandles(provider,
tracer, meter_provider=None, meter=None)`. SDK owners implement `force_flush` and
`shutdown`; the tracer implements `start_span`. A requested meter implements
`create_counter` and `create_histogram`.

Register each top-level SDK owner with `services['own_exporter'](owner)` immediately
on allocation, before another allocation can fail. Do not register an exporter
separately when its SDK provider already owns it. Registration retains partial
resources if construction raises. Returned handles are also registered before
validation. A failed creation or metric-instrument initialization remains owned
until cleanup is acknowledged; failed cleanup can be retried without re-exporting
the terminal call. The shared runtime bounds initialization, flush and disposal.
The built-in trace SDK factory registers a phased owner before opening its HTTP
session. Session, exporter, batch processor and provider receipts survive later
construction, registration and tracer-acquisition failures. Each cleanup phase
is acknowledged only after it returns; a failed phase remains retryable. Direct
factory failures expose the same receipt as `cleanup_owner` on the exception.
Adapter cleanup follows acknowledged signal disposal, then collector leases may
be released. Factories and adapters must not install global providers or read a
second mutable copy of their configuration.

The independently authored built-in adapters are
[OTLP HTTP](../../src/api_agent/telemetry/trace_adapters/otel/provider.py) and
[LangFuse](../../src/api_agent/telemetry/trace_adapters/langfuse/provider.py).
A third provider uses the same contract and requires no core backend-name case.

## Signal configuration

Endpoint precedence uses actual configuration provenance:

1. An explicit application field, resolved through normal CLI/environment/saved
   precedence.
2. The standard signal-specific OTEL endpoint.
3. The standard OTLP base endpoint, appending `/v1/traces`; `/v1/metrics` is appended
   only when the descriptor explicitly admits `metrics_from_base=true`.
4. A typed application default.

A raw dictionary's supplied endpoint is explicit. A resolved conditional default
is not explicit, even when it equals a commonly used address. Signal URLs are
complete URLs and are never suffixed again. LangFuse compatibility does not honor
unrelated generic OTEL destination/header overrides: its explicit project route
and keys remain bound to that backend.

`TRACE_EXPORT_PROTOCOL` overrides signal-specific, then general OTEL protocol
settings, then a typed default. This implementation supports HTTP/protobuf;
gRPC and HTTP/JSON fail before allocation. Per-signal headers override general
OTEL headers; explicit `TRACE_EXPORT_HEADERS` overrides both. Header values are
registered as secrets before SDK allocation and remain out of ordinary reports.

Metrics require `TRACE_METRICS=True` and a declared capability. An explicit
`TRACE_METRICS_ENDPOINT` overrides the standard metrics endpoint. Trace URLs are
never reused or rewritten into metric URLs. Explicit
`TRACE_METRICS_TEMPORALITY` overrides the standard OTEL preference; DELTA and
CUMULATIVE are qualified. Select DELTA where the destination requires it, including
New Relic. Trace-only operation ignores inactive metric temporality settings.
The bundled OTEL profile requires a separate explicit application or
signal-specific metric endpoint; it does not derive metrics from a general OTLP
base or managed Jaeger. Metric detail controls appear only after opt-in.
LangFuse is trace-only and hides metric controls.

Resource identities support `service.name`, `service.namespace`, `service.version`
and `deployment.environment.name`. Explicit `TRACE_RESOURCE_ATTRIBUTES` overrides
`OTEL_SERVICE_NAME`, which overrides `service.name` in `OTEL_RESOURCE_ATTRIBUTES`.
Unsupported custom keys or credential-valued identities fail before allocation.
No environment detector or process-global SDK state is installed.

## Metric revision and accounting

Current vocabulary is pinned to upstream revision
[`95aff9cdf605bbc7d7e8f7c4745a693bd0c4eea8`](https://github.com/open-telemetry/semantic-conventions-genai/tree/95aff9cdf605bbc7d7e8f7c4745a693bd0c4eea8/docs/gen-ai).
It emits an inference-duration histogram in seconds, input/output token counters,
per-physical-operation token histograms, and known cache/reasoning subset counters.
Legacy profile uses `gen_ai.client.operation.duration` and
`gen_ai.client.token.usage` histograms. Names and units never change silently.

Duration describes the terminal logical client operation, including its retries.
Token measurements use each known physical-attempt ledger row once, including
failed and fallback attempts; the final aggregate is not added again. Failed
usage 5 followed by successful usage 7 records 12. Unknown usage is omitted rather
than fabricated as zero, and an explicit incomplete-usage counter records that
limit. This is an intentional conservative policy where upstream advice permits
zero for missing usage. Parent scopes and deterministic tools add no token usage.
Call and physical identities are bounded and deduplicated for the owner's life.

The third-provider tests serialize real trace and metric OTLP with matching
resources and all four trace/log content-flag combinations. Real backend AI-view
acceptance, HTTP/process/system instrumentors and broader propagator/TLS setting
qualification remain separately tracked in the [signal PRD](../prds/implementation_plan_k1a.md).

## Metric delivery and disposal

Metrics use the SDK reader's public infinite-interval mode, without a periodic
reader thread. `record_call` records instruments in memory and performs no HTTP
request. Delivery occurs on explicit owned SDK `force_flush` and on the shared
runtime/session close path. This does not imply delivery after each graph turn
or live periodic dashboard updates; broader live cadence remains open. Trace
batch processing is unchanged.

The metric owner retains exporter, reader, provider and HTTP session separately.
It closes the exporter admission gate and waits for active exports to drain before
acknowledging reader/provider shutdown, then closes the session. Every successful
phase is remembered; failure retains the unfinished phase for retry. One-shot SDK
provider/reader no-op returns cannot replace lower-resource acknowledgement. A
failed or timed-out export drain keeps disposal and collector release pending.
