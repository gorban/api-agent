# GenAI signal and instrumentation conformance

[Collector attribution](implementation_plan_k1.md) · [Local diagnostics](implementation_plan_b1c4a.md)

These pending requirements extend the generic tracing contract. Exporting a
synthetic span alone does not establish application instrumentation compatibility.

- [ ] Pin a supported GenAI semantic-convention revision and test its mapping for operation/provider, requested/served model, finish/error state and token attributes. Use the selected revision's span names and kinds. Keep legacy aliases explicit and versioned rather than universally rewriting provider identity.
- [ ] Export both traces and versioned GenAI duration/token metrics. Qualify legacy `gen_ai.client.operation.duration` / `gen_ai.client.token.usage` compatibility separately from current `gen_ai.client.inference.duration` / `gen_ai.client.inference.usage.*` conventions; do not silently rename metrics on upgrade. Use consistent trace/meter resources and qualify temporality/explicit overrides, including New Relic's documented delta preference.
- [ ] Preserve parent context from the HTTP request through graph/component/node, tool and model/client operations, including streaming, asynchronous tasks and remote boundaries. Define framework-versus-client layers and attribute authoritative leaf usage once; nested instrumentation must not multiply token/cost totals. Manually instrument transports not covered by SDK hooks.
- [ ] Support bounded HTTP server/client visibility and explicitly enabled process/system metrics. Allow probe exclusion and suppress per-chunk ASGI send/receive spans without losing the encompassing streamed request or its terminal state.
- [ ] Define supported standard OTEL endpoint, signal-specific endpoint, protocol, resource and propagator settings with explicit precedence against application configuration. Distinguish an OTLP base endpoint from a complete signal URL. Qualify dependency versions and installed propagators together; fail actionably for unsupported combinations.
- [ ] Reconcile automatic instrumentation with runtime-owned trace/meter lifecycle before enabling it. Avoid duplicate global registration, cross-runtime parenting or competing backend exporters. Retain bounded queues, cancellation, flush/shutdown acknowledgement, disabled-mode inertness and isolated tests.
- [ ] Apply the existing content/credential boundary before SDK queuing for spans, events and metric labels. Instrumentation content switches cannot override LOG_CONTENT. Verify default content omission and explicit content capture using synthetic values; credentials and hidden reasoning remain excluded.
- [ ] Verify real serialized trace and metric payloads through a synthetic collector, source and isolated installed execution. Include ancestry, one-time accounting, resource attribution, stream cancellation, dependency combinations and meaningful rejection controls. A backend conformance check must verify its intended AI view as well as successful OTLP transport. Live export requires separate authorization.

## Public specifications

- [GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) and [metrics](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md): select and pin a supported revision when implementing.
- [OTLP exporter configuration](https://opentelemetry.io/docs/specs/otel/protocol/exporter/) and [Python context propagation](https://opentelemetry.io/docs/languages/python/propagation/).
- [ASGI instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/asgi/asgi.html).
- [New Relic AI Monitoring](https://docs.newrelic.com/docs/ai-monitoring/opentelemetry-ai-monitoring/) and [OTLP metrics configuration](https://docs.newrelic.com/docs/opentelemetry/best-practices/opentelemetry-otlp/).

## Dependency security prerequisite

- [ ] Qualify a mutually compatible API/SDK/exporter dependency set containing `opentelemetry-api>=1.45.1` before enabling inbound trace-context extraction. The [upstream tracestate parsing advisory](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h) affects API versions through 1.45.0. Verify installed versions, the bounded propagation behavior and existing exporter/lifecycle regressions; a semantic-convention upgrade alone does not close this dependency requirement.
