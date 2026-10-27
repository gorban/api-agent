# Deterministic workflow path coverage

[Workflow coverage](implementation_plan_b2b1.md) · [Historical expanded policy](implementation_plan_b2b1a10.md)

The ordinary workflow integration test exercises its real parsed graph through
the selected provider without statistical calibration or a saved baseline.
Historical pilots, bounds and their source identities retain their original
meaning; they do not determine the new test's acceptance.

## Selection and execution

- [ ] Generate fresh canonical version-4 UUIDs and classify them through the same
  native route/readiness function used by the real prompt producer. Discard an
  already covered reachable combination before inference; never edit UUID bits
  or alter returned model values to force a path.
- [ ] Cover all ten reachable combinations: skip with either first-component
  readiness, plus each continuing route with all four first/second readiness
  pairs. Unexecuted second-component readiness is irrelevant to a skipped path.
- [ ] Pass the exact selected UUID into the graph. Assert that real responses
  honor the explicit decisions, actual root routing matches the selected case,
  each required node executes, and requested rejection reaches its real guard
  and internal retry. A planned path or a configured retry limit is not proof.
- [ ] Preserve independent graph/turn/visit/attempt identities, incremental
  evidence, usage/accounting and failures. Duplicate UUID draws cannot consume
  inference or become extra graph executions. Fail execution defects directly;
  never replay failed inference, change accounts/providers or weaken schemas.
- [ ] Bound selection, setup, discovery and graph execution by one configurable
  whole-test deadline, retaining shorter parent/component/request limits. Yield
  during unsuccessful selection so cancellation and progress remain responsive.
  Failure identifies remaining cases safely rather than waiting indefinitely.

## Provider and configuration boundaries

- [ ] Resolve ordinary provider-owned test profiles before setup, preserving
  explicit test overrides and allowed-model admission. Use a shared optional
  workflow output-token setting for provider-specific budgets; generic drivers
  contain no provider protocol branching or calibration-owned request rewriting.
- [ ] Remove ordinary coverage's baseline/fingerprint/cap prerequisites. A new
  provider needs a working supported profile and actual execution assertions,
  rather than an empirical coverage pilot. Keep historical maintenance readers
  distinct from the current integration contract and explain obsolete controls.
- [ ] Reuse production parser, executor, provider and timeout interfaces; do not
  fork production implementations solely to stabilize a test fingerprint.

## Acceptance

- [ ] Deterministic tests cover all cases, duplicate draws, UUID forwarding,
  skipped second components, wrong model/output/route/readiness, genuine retries,
  missing or forged structural evidence, timeout/cancellation and privacy.
- [ ] Causal controls detect removed filtering, replaced UUID forwarding,
  weakened actual-route/retry checks and bypassed overall deadlines.
- [ ] The normal GPT and Bedrock workflow integrations pass with real selected
  providers, normal authentication/accounting and Allure publication. Retain
  failed attempts; no new calibration is required.
- [ ] Complete independent and external review, applicable exact unit coverage,
  installed/relocated resources, provenance/license and public-boundary checks.
- [ ] Human review confirms bounded coverage and its per-case GRAPH evidence.


## Dormant workflow-control scoping

- [ ] Resolve the workflow-only output cap to the shared typed empty default for
  an explicit registry of exact, physically bundled nonconsumer integration cases,
  after provider profile merging. Retain truthful inactive provenance, workflow
  consumer overrides and profile defaults, and conservative values for unknown,
  parameterized or external cases. Never change application values, source fields
  or environment mappings.
- [ ] Prove picker estimation and recorded execution hash the same effective
  per-case settings: changing this dormant cap cannot invalidate unrelated-case
  history, while changing the workflow cap changes workflow compatibility. Check
  explicit empty/value precedence, source provenance, mixed order and concurrency,
  custom-root preservation and causal rejection of removed or overbroad scoping.

- [ ] Interpret implicit collected roots through trusted collection context for
  both bundled provider profiles and dormant-control scoping. Preserve explicit
  roots; resolve unique base/owner repository labels, including installed and
  equal-basename layouts, without inferring ownership from node names. Unknown
  or ambiguous ownership remains unscoped. Verify real collector-shaped base
  and external-owner collisions, picker/recording parity and historical matches.
- [ ] Keep collection and both execution runners on the same authoritative
  repository labels. Preserve unambiguous source/installed labels; disambiguate
  canonical-label collisions without losing base/owner identities. Verify actual
  child working directories and retain explicit provider-root execution.
