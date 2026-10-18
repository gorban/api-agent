# Included Amazon Bedrock LLM provider

[LLM adapters](implementation_plan_f1.md) · [Cost qualification](implementation_plan_f3c.md)

Add an optional public Bedrock adapter through the same replaceable provider
contracts as the included GPT adapter. This is planned work, not an existing
adapter or a claim of live AWS verification.

- [ ] Implement the versioned factory, lazy AWS credential/client lifecycle,
  regional catalog/selection and completion/streaming through supported Bedrock
  operations. Prefer documented Converse/ConverseStream where they cover the
  selected model; explicitly map and test any required model-specific InvokeModel
  routes. Preserve selected model/version, tools, structured output, reasoning,
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
- [ ] Human-review configuration and credential acquisition before separately
  authorizing live AWS catalog/inference checks. Keep live verification explicitly
  unavailable until an appropriate account/permissions and spending approval exist.

Use official [Bedrock inference documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html),
[guardrail behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-how.html)
and [pricing](https://aws.amazon.com/bedrock/pricing/) when implementing. Guardrail
policy charges need not be model-token charges; do not manufacture token counts
to fit them into the existing token categories.
