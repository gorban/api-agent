# Claude reasoning controls and budget-backed effort

[Bedrock Runtime](implementation_plan_f4o.md) · [Reasoning qualification](implementation_plan_f4l.md)

Expose useful reasoning choices through the existing provider catalog and Studio
controls for supported Claude models. A disabled selector must mean that the
selected route has no admitted controls, rather than that all Claude reasoning is
unsupported. This is planned work; the current Converse encoder rejects reasoning
effort and its catalog offers no efforts.

## Requirements

- [ ] Qualify controls by exact selected model/version, routing profile, endpoint
  and API. Distinguish manual thinking-token budgets, adaptive/native effort,
  unsupported controls and unknown capabilities. Do not infer support solely
  from Haiku, Sonnet or Opus family labels, discovery success or a newer version.
  Reuse the provider capability infrastructure; do not create UI-specific rules.
- [ ] Prefer supported native effort controls. For budget-only routes, translate
  the shared low/medium/high intent into provider-owned, configurable token-budget
  presets. Review the proposed numerical defaults before implementation; document
  that presets are relative choices, not equal reasoning quality, latency, cost
  or actual token consumption across models. Native effort must not be replaced
  with an invented budget on routes that reject manual budgets.
- [ ] Keep mapping and defaults in normal provider configuration with conditional
  applicability, help, provenance and existing explicit-setting precedence.
  Resolve the effective choice for each actual selected request, including
  Studio model/version changes and test-specific overrides; never write dotenv
  or mutate global application settings during inference.
- [ ] Resolve budgets against the shared `LLM_MAX_OUTPUT_TOKENS`, explicit lower
  request/node limits, model/context limits and learned smaller output ceilings.
  Preserve answer headroom and required thinking minima. Define deterministic,
  monotonic presets and report any documented reduction; reject an impossible
  choice before dispatch instead of silently changing effort or raising a user
  ceiling. A smaller ceiling learned after a confirmed pre-inference rejection
  must recompute the compatible budget through the existing negotiation owner.
- [ ] Encode supported controls using the selected API's documented fields,
  including Converse `additionalModelRequestFields` where applicable. Qualify
  existing Responses/Completions routes independently. Do not introduce Messages
  API or change endpoint/API/model/account implicitly to make a control work.
  Preserve upstream permission and unsupported-feature diagnostics.
- [ ] Keep omitted/provider-default intent distinct from off. Offer off only when
  the selected model/API supports disabling thinking; omission must not be
  presented as proof that thinking is disabled. Validate temperature, sampling,
  forced-tool and structured-output compatibility before side effects where the
  incompatibility is established. Keep allowed output fallback explicit.
- [ ] Enable Studio choices from the admitted provider catalog, preserving exact
  execution IDs and current per-version/family selection rules. Explain supported
  budget-backed choices and disabled/unknown controls without hardcoded Claude
  logic in the browser. Any shared DTO extension must remain generic and receive
  consumer-contract review before adoption.
- [ ] Preserve the selected thinking mode and compatible native continuation
  across tool results, internal retries and later user turns. Extend current
  signed/opaque continuation support where required before claiming reasoning
  tool-call parity; never discard required state to make a text-only case pass.
  Retain append-only histories, cache ordering and identity/replay guards.
- [ ] Record requested effort, native mode or derived budget, effective total
  output ceiling and mapping provenance with normal call/GRAPH evidence. Preserve
  available reasoning-token usage, timing and estimated cost; unknown usage stays
  unknown. A reasoning summary is not a measurement of billed thinking tokens.
  Keep raw/encrypted reasoning out of displayable summaries and retain existing
  content flags and credential protection.
- [ ] Preserve workflow/component/provider deadlines, streaming ownership,
  cancellation, heartbeats, cleanup and transport-retry accounting. Configuring
  deeper reasoning does not create an unlimited turn or consume application
  retry counters merely to negotiate a supported control.

## Acceptance and delivery

- [ ] Review the model/API capability matrix, budget preset values, answer
  headroom policy and any generic DTO change before coding dependent behavior.
- [ ] Verify budget-only Haiku/Sonnet versions, adaptive/native-effort
  Sonnet/Opus versions and available newer Haiku controls independently. Include
  unknown/new versions, regional profiles, unsupported routes, explicit overrides,
  small ceilings, model switches, concurrent requests and test-order isolation.
- [ ] Prove actual request serialization and complete/stream decoding, tool-result
  continuation, fallback compatibility and error/usage preservation. Add causal
  rejection controls; do not satisfy this gate by enabling the dropdown alone.
- [ ] Complete applicable exact unit line/branch and frontend coverage, installed
  and relocated resources, public-boundary/license checks, independent and
  external review. Use authorized normal Run local/integration/E2E requests for
  live qualification; preserve failures and actual selected identity/settings.
- [ ] Human acceptance: choose supported Claude versions in Studio, change effort,
  inspect the effective native control or token budget in evidence, complete a
  normal answer and a tool continuation, and confirm unsupported choices remain
  clearly explained. No promise that every high-effort call uses more tokens.

## Public references

- [AWS extended thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html): manual-budget constraints and interactions.
- [AWS adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html): version-specific native controls and Converse fields.

Recheck the exact model/API documentation during implementation. These references
do not establish permission, transport support or measured behavior in an account.
