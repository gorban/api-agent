# Resource-pack tracing providers

[GenAI conformance](implementation_plan_k1a.md) · [Resource packs](../resource-packs.md) · [Complete execution traces](implementation_plan_k1a1.md)

The version-1 candidate implements captured pack-selected providers, builtin
compatibility adapters, optional dashboards and managed infrastructure, and
GenAI metrics. Qualification and independent review remain open below; this does
not complete the broader signal conformance requirements.
The [version-1 contract](implementation_plan_k1a2a.md) defines the implementation
interface and its remaining acceptance gates.

[Provider-owned configuration and progressive discovery](implementation_plan_k1a2c.md)
tracks the default-provider and canonical export-endpoint follow-up.

## Contract and configuration

- [ ] Agree a versioned tracing-provider declaration and factory/lifecycle contract before implementation. A resource pack must be able to select an implementation without editing core backend-name branches. Preserve existing OTEL/LangFuse configurations through explicit compatibility adapters.
- [ ] Reuse existing owner-relative provider discovery, typed nested configuration, conditional defaults/requirements and ordinary CLI/environment/saved-value precedence. Help, configuration review and test collection must remain lazy, without importing executable factories, authenticating or starting services. Reject unsupported selected capabilities actionably before allocation.
- [ ] Separate export capabilities from optional dashboard capabilities. Support exporters without a UI, remote collectors without local infrastructure, and dashboards without login automation. Declare supported signals, field mappings, readiness and authentication requirements; do not infer inference access or feature support from a reachable endpoint.

## Shared capture and ownership

- [ ] Keep canonical graph/node/tool/LLM observations, identities, timing, outcomes and leaf accounting in shared runtime capture. Providers map that data into their supported GenAI attributes, metrics and native input/output views; they must not require per-flow wiring or introduce a second execution recorder. Coordinate the complete-operation field matrix with provider capability/version evidence.
- [ ] Preserve TRACELOOP_TRACE_CONTENT as the shared trace-content authority and LOG_CONTENT as the independent log/Allure authority. Apply credential and hidden-reasoning exclusions before provider/SDK queues; provider-specific switches cannot bypass them. Preserve explicit unknown, unavailable and incomplete states.
- [ ] Reuse existing captured infrastructure planning, recursive dependencies, readiness and shared leases for optional managed collectors. Acquire only the selected closure; remote export must not start a local stack. Keep exporter flush/disposal before last-use collector release, retained volumes, retryable cleanup ownership and bounded deadlines. Do not add another scheduler or resource owner system.
- [ ] Let providers supply an optional dashboard URL, readiness check, login preparation and explicitly designated human-login display values. The shared viewer owns headed browser/CDP presentation and command lifetime until Ctrl+C, including after browser closure. Literal credential display is limited to the expressly designated dashboard login on the requested command console; exporter/API tokens must never be selected implicitly or enter ordinary diagnostics, artifacts or events. Preserve authenticated identity and isolate concurrent owners.

## Documentation and acceptance

- [ ] Extend the existing progressive documentation: README → resource-pack overview → tracing-provider guide → contract/examples. Keep tools, LLM providers, UI providers and nested configuration discoverable from the same overview. Explain export versus dashboard capabilities, configuration contributions, optional recursive local services, remote-only operation and cleanup ownership. Distinguish delivered behavior from planned extensions.
- [ ] Supply independently authored pack examples and tests proving a third backend can be selected without core edits. Include export-only, dashboard-only/no-login, authenticated dashboard and managed recursive-infrastructure capabilities, plus missing optional capabilities and remote destinations. Verify disabled/lazy admission, precedence, concurrency, failure/cancellation, flush, retained data, literal authorized login display and no exporter-token disclosure.
- [ ] Verify serialized GenAI fields/native panels and all four content-flag combinations through the generic capture path, including both parsed-flow executors and named tools. Complete causal controls, exact applicable coverage, source/installed/relocated resources, documentation links, boundary/license checks, independent/external review and separate human acceptance. Real authenticated backend qualification requires its normal authorization.

[Captured Studio/test collector requirements](implementation_plan_k1a2b.md) define
the protected snapshot follow-up and its independent acceptance gates.
