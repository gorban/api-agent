# Tracing

Tracing uses optional TRACE_MODE and TRACE_TARGET configuration. Empty mode
disables exporters and local tracer services. OTEL selects a complete OTLP HTTP
traces endpoint; LangFuse selects its server address. Conventional loopback
targets can acquire verified managed infrastructure; custom endpoints remain
externally owned. Viewer and ingestion addresses are distinct.

## Delivery and remaining work

- [x] Deliver optional declarations, lazy manual exporters, bounded shutdown and
  managed backend leases. Synthetic backend checks establish retained export,
  authentication and reuse, not full application signal conformance. Preserve
  the [backend lifecycle requirements and verification](implementation_plan_k3.md).
- [x] Close technical verification of the bounded manual workflow/client, local
  dashboard and diagnostic join under
  [independent content controls](implementation_plan_k1b.md) and
  [startup dashboards](implementation_plan_k2.md). Core Run local acceptance
  remains separate and recorded.
- [ ] Complete [collector attribution](implementation_plan_k1.md) and
  [GenAI instrumentation conformance](implementation_plan_k1a.md), including
  broader metrics, propagation, automatic instrumentation and downstream AI views.
  A working manual local trace does not establish deployed parity.

## Content and ownership

LOG_CONTENT controls permitted application logs, local diagnostics and Allure
details/screenshots. TRACELOOP_TRACE_CONTENT independently controls permitted
payloads for both tracing backends. Both default False; credentials and explicitly
classified internal state remain excluded. Scrub before capture/queues, preserve
shared execution/accounting identity, and never retrieve withheld content from a
dashboard.

Dashboard account selection is separate from LLM provider authentication. Local
stacks retain verified lease/volume ownership; remote targets start no containers.
The public [configuration guide](../tracing.md) describes supported settings,
startup tabs and the bounded manual scope.

## Deferred human review

Human acceptance of these bounded additions remains deferred; earlier core Run
local acceptance is unchanged. The detail checkpoints retain unchecked acceptance.

- [Trace signal requirements](implementation_plan_k1.md)
- [Startup dashboards](implementation_plan_k2.md)
- [Backend lifecycle and acceptance](implementation_plan_k3.md)
