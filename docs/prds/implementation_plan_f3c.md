# Mixed-model pricing and quota attribution qualification

[Accounting contracts](implementation_plan_f3.md) · [Workflow comparisons](implementation_plan_b1c2.md)

## Scope and distinctions

Each inference call can select its own model and requested reasoning effort. Preserve requested/catalog/actual model, explicit unknown revision, effective reasoning, rate provenance, cache/usage categories, provider/account identity and attempt identity. A list-price equivalent is not a measured subscription charge. A suite-wide quota observation cannot by itself identify each model's actual quota contribution.

The current implementation prices calls separately, sums their list equivalents and uses those weights for quota allocation. Calibration supplies one factor for a compatible set of model/version/reasoning signatures, not an independently learned factor for every signature. Different proportions within that set can share the same factor. Current picker matching refuses to average differing call signatures. These are explicit assumptions/limits to qualify, not evidence of per-model measured charges.

## Joined deterministic qualification

- [ ] Inventory integration/workflow tests and request overrides by model, version and reasoning; distinguish single selected-model smoke tests from deliberately mixed graphs. Add independently authored mixed declarative fixtures through the real loader when format conversion is ready. No paid inference is needed to prove accounting arithmetic or persistence.
- [ ] Prove multiple calls within one test and across tests through real controlled provider/worker IPC, finalized Allure accounting, rebuilt SQLite, prediction and completed-result/category/step consumers. Use distinct synthetic model/version prices with equal token counts so applying one default rate cannot pass. Cover effective reasoning overrides, aliases versus actual IDs, unknown revisions, missing prices, nonstandard tiers, cache categories, context thresholds, retries and failures after usage capture. Costs reconcile without double-counting inclusive token subsets.
- [ ] Prove qualified per-call reported charges retain priority for API-key billing; permitted fallback applies only to the unbilled call. Preserve original charge, frozen list equivalent, quota allocation and derived estimate separately. Unknown rates or unsupported mappings stay unavailable instead of borrowing another model/provider's price.
- [ ] Prove shared-account quota allocation uses summed per-call list-price weights, with zero/rounded deltas, calibrated history, changing mixture proportions, resets, overlaps and distinct allocations. Disclose the common-factor assumption; do not label allocated model/node costs as individually measured charges. Preserve existing precision/qualification and pre-Start freezing rules.

## Cohorts and optional accuracy work

- [ ] Qualify provider and billing-route separation with identical model/version,
  reasoning, token counts and test identity but distinct provider costs. Predictions,
  quota fits, reported-charge averages and rate snapshots must not cross provider
  cohorts. Existing provider identity fingerprints the factory file contents and
  symbol, not its path/display name or all imported helpers. Existing route/auth
  separation depends on declared cost-affecting settings. Verify relocated copies
  remain compatible while distinct effective billing/guardrail profiles do not;
  require a stable non-secret profile identity where that fingerprint is insufficient.
- [ ] Cover the same adapter with changed endpoint, region, guardrail/version,
  inference profile, service tier, billing policy or request transformations.
  Declare relevant non-secret settings `cost_affecting=true`; missing or unknown
  required profile evidence stays unavailable. An unobserved server-side policy
  change cannot be inferred from model identity alone: retain the uncertainty,
  split cohorts when identified and never retroactively rewrite original costs.
- [ ] Distinguish provider-added charged model tokens from ancillary fixed/unit
  charges. Qualified reported totals and provider-specific quota calibration may
  include overhead, but a token-only list equivalent may omit it. Preserve any
  unattributed remainder and unknown costs; do not invent guardrail token counts
  or label proportional estimates as itemized measured charges. Blocked requests
  with zero model tokens may still incur charges. Extend generic charge evidence
  only as needed under a validated versioned contract. Keep the accepted shared
  calibration factor within each compatible provider/profile; sharing a model name
  never authorizes sharing that factor across providers.
- [ ] Define prediction cohorts for explicit model/version/reasoning selections and dynamic workflow paths. Investigate current all-signatures-conflict behavior before changing it: preserve separate cohorts, strict compatibility and honest unknown predictions when the next graph path is unknowable. Do not merge differing efforts merely because their per-token rates match; their usage distributions may differ.
- [ ] Validate whether one factor is accurate enough as model/effort proportions change. The shared factor is the accepted policy. Separate model/effort factors are outside this delivery; reconsider only on a new accuracy requirement with identifiable, qualified observations. In particular, mixed suites whose model weights always move together cannot identify separate factors. Record uncertainty, required sample coverage and a materiality decision before implementing a more complex estimator. Never generate paid calls solely to fit factors without separate authorization.
- [ ] Keep declared pricing dimensions explicit. Current standard text pricing has model/token-category/context dimensions; reasoning effort is recorded as a usage/cohort setting, not an invented price multiplier. A provider charging differently by effort, tier or route needs validated additional rate dimensions or unavailable pricing. Multiple provider/quota accounts require separate meter observations before combining totals; a shared suite delta is insufficient.

Qualify this before workflow step-cost/history overlays are accepted. Preserve existing valid closures; this is an additional joined-coverage and accuracy checkpoint. Require meaningful mutations, applicable exact unit coverage, installed/relocated fixtures, independent review and public-boundary gates when implemented.
