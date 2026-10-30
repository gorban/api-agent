# Bedrock transport, capabilities and cost qualification

[Bedrock requirements](implementation_plan_f4.md) · [LLM adapters](implementation_plan_f1.md) · [Cost qualification](implementation_plan_f3c.md)

Add an optional public Bedrock adapter through the same replaceable provider
contracts as the included GPT adapter. The controlled Mantle runtime is implemented
under [its bounded matrix](implementation_plan_f4d.md); pricing and the broader
requirements below remain open. The approved [list-price phase](implementation_plan_f4k.md) qualifies bounded accounting; earlier live authentication/catalog evidence does not qualify prices.

- [x] Standalone SDK workers launched from source must resolve their own api-agent
  distribution without an installed wheel or inherited PYTHONPATH, including
  catalog/error paths before inference. Verify with a clean interpreter so an
  older installed package cannot mask a broken source-worker import.

  Verified with a clean interpreter and a synthetic successful catalog from an
  arbitrary directory, without account access. The regression previously emitted
  a protocol error because its error handler also lacked the source package.
  Full units: 21,022 plus 27 subtests, 100% measured line/branch coverage;
  independent review passed. Live selected-account acceptance remains separate.

- [ ] Implement the versioned factory, lazy AWS credential/client lifecycle,
  regional catalog/selection and completion/streaming through supported Bedrock
  Mantle HTTP operations. Support independently selected Responses and Chat
  Completions formats under the [Mantle-only contract](implementation_plan_f4d.md).
  Retire active Converse/ConverseStream and model-specific InvokeModel routes.
  Preserve selected model/version, tools, structured output, reasoning,
  caching, errors, retries, timeout clipping and exact asynchronous disposal.
  Advertise only capabilities actually supported by that model/route.
- [ ] Contribute typed provider-local configuration and optional dependencies.
  Support the documented AWS credential chain without copying credentials into
  source or ordinary config/evidence. Authenticated catalog and inference share
  bounded lazy setup; help, configuration and cached predictions do not start
  login or AWS calls. Document configuration, permissions, prerequisites and
  installation without assuming one operating system or credential mechanism.
- [ ] Define non-secret cost compatibility for provider/billing route, region,
  model or inference-profile selection, service tier, effective guardrail policy
  and version, and any request transformation affecting charged usage. Mark
  applicable settings `cost_affecting=true`; preserve unknown profile identity
  instead of pooling it with another profile. Do not use raw credentials as an
  accounting identity or depend on a renamed filesystem path for separation.
- [ ] Supply validated provider-owned price snapshots/provenance and optional
  reported-charge/usage metadata. Make them available to picker predictions and
  frozen suite accounting through generic contracts; the current GPT-only cached
  price-source selection must not become a silent cross-provider fallback.
  Never substitute a direct model vendor's rates for Bedrock rates solely because
  the model name/version matches. Respect region, inference route/tier and price
  epoch; missing prices remain explicit.
- [ ] Capture actual reported input/output/cache/reasoning usage without inventing
  visibility into hidden processing. Distinguish additional charged model tokens
  from non-token service/guardrail charges. Preserve qualified total charge and
  any unattributed remainder separately from list-price token contributions.
  Unknown ancillary costs cannot be called an exact total; zero model tokens
  do not establish zero spend for a blocked request with billable guardrail work.
  Extend generic accounting only with a validated versioned contract where needed;
  preserve immutable original evidence and CSV compatibility.
- [ ] Join controlled catalog/HTTP or SDK/stream/credential lifecycle tests,
  same-model cross-provider cost isolation, differing guardrail profiles and
  blocked/failed/retried requests. Verify finalized Allure, SQLite rebuild,
  predictions and completed costs, plus installed/relocated package behavior.
  Use distinct synthetic charges/rates so borrowing another provider cannot pass.
  Require meaningful mutations, applicable exact coverage, independent review and
  package/license/public-boundary gates. No new paid inference is authorized.
- [ ] Human-review configuration and credential acquisition before authorizing
  agreed live authentication/catalog checks with an appropriate account and
  permissions. Authorize inference separately with explicit spending limits;
  unavailable live paths remain explicitly unverified.

Use official [Mantle endpoint documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html),
[guardrail behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-how.html)
and [pricing](https://aws.amazon.com/bedrock/pricing/) when implementing. Guardrail
policy charges need not be model-token charges; do not manufacture token counts
to fit them into the existing token categories.

Runtime-only inference profiles, routing and Guardrails are outside the Mantle target. Earlier requirements for those surfaces are retained here as historical scope, not current acceptance gates. Qualify Mantle model/API/path-prefix/region and any supported ancillary charges independently; a similarly named model does not establish feature or pricing equivalence.

## Blocking design decisions

- [ ] Resolve any undecided credential/configuration, capability or accounting contract with the user under the [design-decision requirements](implementation_plan_o.md) before implementing dependent behavior. Existing provider boundaries, controlled qualification and separate live-use gates remain applicable.
