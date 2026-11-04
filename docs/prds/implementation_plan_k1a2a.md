# Tracing-provider contract version 1

[Requirements](implementation_plan_k1a2.md) · [Signal conformance](implementation_plan_k1a.md)

## Inert admission

`TRACE_PROVIDER` is an ordinary `file.py:create_tracing_provider` selector.
Its bundled default contributes TRACE_MODE and nested exporter/dashboard settings
through owner-relative discovery. A blank bundled mode is inert. Custom selectors
replace bundled controls while preserving ordinary CLI/environment/saved-value
precedence. Static version-1 descriptors remain supported; the bundled provider
uses bounded declarative profiles described in the
[configuration requirements](implementation_plan_k1a2c.md).

An adjacent `tracing-provider.toml` declares `version=1`, a bounded stable `id`,
`signals` (`traces`, `metrics`), and native `input_attribute`/`output_attribute`
keys supported by the shared mapping contract. Optional dashboard declarations
name a typed URL setting, readiness setting and explicitly designated human-login
username/password settings. Exporter secrets are declared separately and cannot
be designated for console login display. Dashboard absence is supported, including
remote/export-only providers. Infrastructure uses the existing adjacent
`infrastructure.toml`, captured dependency closure, typed activation/endpoints,
shared readiness and lease ownership; providers do not start collectors themselves.

Admission captures the descriptor and executable source closure without imports.
Unsupported versions, shapes, native mappings, requested signals, dashboard or
login capabilities fail before factory allocation. Help, review and collection
never execute a factory or allocate infrastructure.

## Executable boundary

A direct synchronous `create_tracing_provider(config, services)` returns a
version-1 adapter. `create_exporter(plan)` returns `ExportHandles` containing an
SDK-compatible trace provider/tracer and optional meter provider/meter. The frozen
plan contains admitted endpoints, headers and the same sanitized resource for both
signals. Providers support only declared signals. They may not change shared
content authority or install process-global tracing/meter providers.

The runtime opens the binding only after operation acceptance. Shared capture
continues to own graph/node/tool/model identity, timing, outcomes, sanitized
content and authoritative leaf accounting. `record_call(structural, elapsed,
outcome, *, attempts=None)` receives one terminal logical LLM observation and
the authoritative physical-attempt usage ledger; it has no
content or arbitrary metric-label channel. Core deduplication prevents parent or
observer replay from multiplying usage.

Existing bounded trace and meter flush/shutdown precedes adapter `aclose`
and last collector-lease release. Failed cleanup retains the same retryable owner;
retries never export a terminal leaf again. Dashboard browser presentation and
command lifetime remain with the shared viewer. Only expressly designated human
login values may be printed on that requested command console; exporter tokens
remain excluded from diagnostics/events/artifacts.

## Compatibility and acceptance

Built-in compatibility adapters preserve explicit OTEL/LangFuse configuration and
native views. They advertise trace-only ingestion unless a metric destination is
explicitly supported; no metric URL is inferred from a trace-only backend.

- [ ] Implement declaration, captured factory/config discovery and lazy binding.
- [ ] Integrate shared runtime, optional dashboard and existing infrastructure leases.
- [ ] Qualify synthetic third-provider export-only/remote, dashboard without login,
  authenticated dashboard and recursive managed-infrastructure cases.
- [ ] Qualify metric serialization/resources/once-only accounting and OTLP precedence.
- [ ] Complete causal controls, exact coverage, installed/relocated resources,
  boundary checks and independent/external review. Human backend-view acceptance
  remains separate.

Remote trace-only bindings preserve the original opaque admitted pipeline identity
while owning a separate trace exporter without a second metric SDK. Unsupported
propagation is rejected; no raw endpoint, credential or filesystem identity is
transmitted. Optional HTTP/system signals and broader transport qualification
remain pending under signal conformance.

Metric delivery is bounded to explicit owned SDK flush and runtime/session close;
the SDK reader uses public no-ticker mode. Per-turn or periodic live delivery is
not implemented. Qualification must retain exporter/reader/provider/session phase
acknowledgements across transient failures and drain active HTTP exports before
disposal, using real SDK cleanup semantics rather than repeatable SDK fakes.

Trace startup must retain each successfully allocated session, exporter, processor
and provider before later public SDK calls can fail. Built-in adapters register a
phased owner before session allocation. Direct factory failures retain the same
`cleanup_owner` receipt; acknowledged phases are not repeated after a retry.
Qualification covers provider/processor construction rejection, processor
registration, tracer acquisition, cancellation, session-close retries and active
HTTP export drain. Resources hidden by a third-party constructor that raises
before returning are outside this public lifecycle guarantee; constructors use
ordinary SDK APIs without private-field recovery or custom allocation.
