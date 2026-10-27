# Included Amazon Bedrock LLM provider

[LLM adapters](implementation_plan_f1.md) · [Cost qualification](implementation_plan_f3c.md)

Deliver Bedrock through replaceable provider contracts, with adapter-owned configuration, credentials, catalog, transport, pricing and lifecycle. The complete 18-case ordinary defaults catalog passed; final artifact/review passed; human and broader live gates remain open. The [acquisition foundation](implementation_plan_f4a1.md) defines replaceable session adapters.

Target only bedrock-mantle with [OpenAI-compatible APIs and three authentication choices](implementation_plan_f4d.md). Offline tests qualify both formats and reusable acquisition ownership; broader live capabilities remain open.

- [x] Qualify [provider-selected reasoning and integration tests](implementation_plan_f4l.md).
- [x] Qualify [snapshot-only reuse](implementation_plan_f4j.md) and [Mantle list-price accounting](implementation_plan_f4k.md).
- [x] Repair [cached-session reuse and output-copy hints](implementation_plan_f4i.md) after the reported restart failure.
- [x] Repair [session setup and simplify adapter selection](implementation_plan_f4h.md) following the reported public-workflow failure.
- [x] Address [session reuse and attended credential feedback](implementation_plan_f4g.md), including Completions compatibility and discoverable native login settings.
- [ ] Agree and implement [credential acquisition and interaction](implementation_plan_f4a.md): SDK/profile resolution, direct/federated login, selected password-manager account, key credentials, refresh and same-identity recovery. Keep acquisition independent of billing.
- [ ] Implement [transport, capabilities and cost qualification](implementation_plan_f4c.md), preserving all model/route/region, guardrail, usage and provider-rate requirements. Never substitute another provider's prices.
- [ ] Qualify [workflow integration and acceptance](implementation_plan_f4b.md): actual flow.md.html parsing and LangGraph execution, deterministic provider tests, installed resources, asynchronous ownership and credential-safe accounting evidence.
- [x] Qualify [selected unattended key modes](implementation_plan_f4f.md) through the existing public LLM request with approved live tests and independent/external review; human acceptance remains in the child.
- [ ] Reach the [real public workflow checkpoint](implementation_plan_f4e.md) through Run local or the existing LLM integration test before human review; authentication/catalog or standalone fixtures are insufficient.
- [ ] Resolve remaining authentication, configuration, capability and accounting decisions under the [design-decision requirements](implementation_plan_o.md) before implementation. BEDROCK_AUTH_MODE=session|access_key|api_key and password-manager fields are agreed; other proposals do not establish support.
- [ ] Review non-secret configuration and credential setup before live authentication. Authorize inference spending separately, with explicit scope and limits. Authentication readiness does not authorize inference.

Qualification may use a bounded model/route matrix. Reject unsupported features explicitly; SDK calls alone do not establish workflow or production acceptance.

## Integrated qualification

- [x] Complete [default-driven ordinary integration delivery](implementation_plan_f4l3.md): unit/coverage, normal catalog, installed-resource, boundary and independent/external review gates passed. Preserve application/shared behavior and each earlier result's source/configuration scope.
- [ ] Human acceptance through ordinary launcher defaults remains separate.

- [ ] Qualify [model-specific prompt caching](implementation_plan_f4m.md) with eligible inference access.
