# Endpoint-only tracing and shared collector attribution

[Tracing](implementation_plan_k.md) · [Reporting lifecycle](implementation_plan_h2a2.md).

## Agreed routing

- [x] Select tracing using existing `TRACE_MODE` and one `TRACE_TARGET` URL. OTEL takes the OTLP collector traces endpoint; LangFuse takes its server address, from which its exporter derives the documented ingest path. No reporting UUID, registry handshake or additional managed/external selection field is required.
- [ ] Preserve conditional local defaults and explicit configuration precedence. A custom or Kubernetes service endpoint starts no local tracer stack and is never stopped by local cleanup. Keep collector ingestion separate from the viewer address; do not substitute a browser UI URL for an exporter endpoint. Verify path derivation, TLS and protocol handling without double-appending ingest paths.
- [ ] Local defaults may start/reuse verified locally managed services. Process/session leases and container/volume ownership are internal management mechanisms, not required reporting configuration. Endpoint availability alone never authorizes adoption or deletion.

## Application and environment attribution

- [ ] Set OTEL `service.name` from the resolved application/resource-pack service metadata rather than always naming the base application. Support standard `OTEL_SERVICE_NAME`/resource metadata overrides consistently; document precedence and validate/scrub values before SDK allocation.
- [ ] Populate `deployment.environment.name` through standard resource configuration or deployment-supplied metadata. Preserve service namespace, version and instance attribution when supplied safely; carry suite/test/attempt/session correlation on the appropriate spans. A shared collector can accept different services in one environment without changing their destination URL.
- [ ] Preserve safe Kubernetes resource attributes supplied by deployment/collector enrichment for infrastructure correlation. Do not require another tracing target or a custom registration service. Do not synthesize unavailable deployment metadata or put credential/account evidence into resource attributes.

## AI observability compatibility

- [ ] Emit the supported OpenTelemetry GenAI semantic conventions for LLM operations, requested/served LLM identifiers, latency, finish reasons, errors and token usage. Link agent steps, tools and inference calls through parent/child spans and propagated context. Pin and test the supported convention/instrumentation versions, including manually instrumented provider transports; SDK auto-instrumentation alone may not observe those transports.
- [ ] Verify the resulting spans can populate New Relic AI monitoring through standard OTLP. No New Relic APM agent is required for this route. In production, the shared collector normally owns downstream ingest credentials/export configuration; direct export must supply required backend credentials through the existing privacy boundary, never embed them in URLs or telemetry.
- [ ] Preserve `LOG_CONTENT=False` by default. Explicit True may include request/response/tool content after credential scrubbing, before SDK queues and export. Instrumentation environment flags cannot bypass the application's content policy. Trace metadata and errors remain useful without content.

## Acceptance

- [ ] Deterministically verify URL-only export to a synthetic shared collector, correct application/environment attribution for two services, context linkage, token/error fields, remote-target no-container behavior and conditional backend authentication.
- [ ] Verify LangFuse address-to-ingest-path conversion, exporter flush/cancellation, disabled-mode inertness, unsafe resource metadata exclusion and credentials/content scrubbing across worker/parent boundaries. Retiring obsolete UUID routing must not weaken local container ownership checks.
- [ ] Complete applicable exact unit coverage, wheel/license/public-boundary checks and independent review before code commits. Review a sample scrubbed span and deployment configuration before any separately authorized live production export. Do not start paid inference merely to validate instrumentation.

## Public protocol sources

- [New Relic OpenTelemetry AI monitoring](https://docs.newrelic.com/docs/ai-monitoring/opentelemetry-ai-monitoring/): OTLP/GenAI visibility, service attribution and optional content capture.
- [New Relic OTEL resource requirements](https://docs.newrelic.com/docs/opentelemetry/best-practices/opentelemetry-best-practices-resources/): service identity and infrastructure correlation.
- [OpenTelemetry resources](https://opentelemetry.io/docs/concepts/resources/) and [deployment attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/deployment/): standard service/environment metadata.
- [OpenTelemetry GenAI conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/): authoritative attribute and span contracts. Recheck current stability and supported library versions when implementing.
