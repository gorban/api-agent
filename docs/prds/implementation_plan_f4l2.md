# Bedrock integration-suite readiness

[Provider-selected integration qualification](implementation_plan_f4l.md)

Make every ordinary integration test executable with the included Bedrock LLM
provider through the public launcher. Maintain the existing assertions, workflow
provenance, accounting, ownership and cleanup; do not manufacture success by
skipping unavailable capabilities or changing failed observations.

- [x] Inventory the ordinary integration catalog, its effective provider/model,
  API, browser and workflow prerequisites. Distinguish controlled tests from
  actual provider calls and provider-specific optional suites.
- [x] Use explicit documented configurations and settings reruns: Responses for
  its qualified reasoning/Studio and ordinary tests; Completions for the GLM
  structured workflow. Full coverage is the union of those selected successful
  runs, not a promise for every setting combination. Do not silently switch
  routes or add a profile-routing layer in that explicit-matrix checkpoint.
  The subsequent operation-scoped delivery below supersedes this limitation.
- [x] Verify actual selected workflow documents against committed provenance
  through the public launcher. Do not assume that an edited shared configuration
  is an admitted physical workflow input. Preserve the provenance checks; a
  private snapshot or disabled reporting does not establish actual readiness.
- [x] Freeze current source/configuration/runtime identity and refresh the GLM 5
  Completions calibration with the normal maintenance command: 20 contiguous
  graphs, at most 80 physical calls, 1024 output tokens per call and a conservative
  2 USD reserve ceiling. Preserve failures and the older report. Independently
  verify observations, fingerprint and exact statistics before promoting only
  the matching Bedrock entry; retain GPT entries and the fixed safety margin.
- [x] Refresh the calibration once more after the rejected-session recovery
  repair changes its sampling-source fingerprint; retain the earlier successful
  pilot and pre-inference recorded failure unchanged.
- [x] Run normal workflow node coverage against the promoted fingerprint, then
  every ordinary integration case through the launch-owned worker path with
  real configured Bedrock access. Keep failed runs and account for every call;
  use bounded requests and stop for unavailable authentication or scope limits.
- [x] Recover an expired or rejected selected session through the visible
  attended sign-in interface, retaining identity assertions and avoiding a
  different profile or account. An unusable owned snapshot must not block
  native refresh or attended recovery solely because its local expiry is later.
- [x] Repair demonstrated provider/readiness/UI failures with focused tests,
  exact new/changed line and branch coverage, causal controls, Sol low
  implementation and distinct Sol high adversarial review.
- [x] Reconcile installed/relocated resources, source/package identity, public
  boundary and the named external PR-style review before human acceptance.
- [ ] Human review through the normal launcher and selected Bedrock settings.

At the earlier explicit-matrix checkpoint, the catalog had 11 modules and 15 cases. Seven exercise provider catalog,
inference, reasoning, browser or workflow paths; eight are controlled checks.
Document the qualified matrix and selectors in README and provider guidance.
At that checkpoint, global API profiles could not cover every case in one unchanged run:
Responses lacks the qualified structured-workflow selection, while Completions
lacks qualified reasoning-counter and Studio selections. The earlier reasoning
smoke pass remains evidence, not blanket suite acceptance. The final four-pass GLM pilot completed 20 graphs and 35 physical calls.
Independent receipt, fingerprint, observer, exact statistics and non-target
preservation checks qualified the promoted 30-run bound (26 minimum plus four
margin). The earlier reports and failures remain retained separately. The actual
recorded Responses run passed 14/14; Completions workflow passed 1/1 with two
calls and all six nodes in one graph. Allure publication passed for both.

The first recorded workflow attempt reached the selected provider, published its
Allure result, and failed before inference because STS rejected the cached
session with ExpiredToken. The focused recovery repair retains selected identity
binding and permits native refresh or visible attended recovery for this
specific authentication rejection. Source-dependent calibration must be
refreshed again after the repair; that earlier completed pilot is retained unchanged. The subsequent
four-pass pilot and normal coverage completed that refresh.

The fresh two-pass pilot stopped at graph 13 after 12 complete graphs and 26
physical calls because the first component guard rejected both completed
replies. Its prefix is not a successful calibration. Offline correction-path
checks found no assembly or encoding defect. Predeclare one four-pass workflow
qualification using the existing retry control, leaving the ordinary default
at two. Keep schemas, acceptance predicates and deadlines unchanged. A fresh
20-graph pilot is bounded at 160 physical calls, 1024 output tokens and 4 USD
conservative reserve. Preserve every failed batch; another exhaustion requires
structural guard diagnosis before any repeat or settings change. Derive the
normal workflow call bound from the new independently reviewed graph cap.

The normal application graph initially rejected the uncommitted shared
configuration. The required configuration and two derived SVG semantic
provenance comments were recorded through normal commit checks; SVG graphics
are byte-identical. The complete post-repair Responses rerun passed all 14
cases, rather than combining the earlier 13 passes with a synthetic substitute.
Focused configuration validation passed 492 tests in an isolated real checkout;
the shared-worktree hook-warning failures remain recorded as environmental.

Final wheel/source-archive byte checks and installed/relocated synthetic provider
checks passed, including all 18 new session-recovery cases in each layout.
The installed checks verify stored baseline arithmetic and packaged resources;
the separate source-launch matrix establishes the 15 real integration passes.
Distinct adversarial and final external PR-style review approved this qualified
checkpoint with no actionable findings. Human acceptance and main integration
remain open.

## Default-driven integration acceptance

The integration follow-up supersedes the PoC-only manual API-switching strategy
above. Reuse the current generic per-test configuration override mechanism if
one exists; provider-specific test choices belong to provider declarations.

- [x] With the included Bedrock provider selected and authentication configured,
  default settings must supply qualified profiles for all ordinary integration
  tests. Users must not manually switch application API mode/model between tests.
- [x] Each test gets an operation-scoped configuration for its model, API,
  generation controls and timeouts. Preserve the ordinary application defaults,
  selected authentication identity and user dotenv; do not mutate shared state
  or allow concurrent test profiles to leak into one another. Generic test
  drivers must not contain Bedrock names or protocol-specific branching.
- [x] Define and test precedence and provenance: explicit test-specific overrides
  remain authoritative, ordinary application settings retain their own scope,
  and test model choices still obey LLM_ALLOWED_MODELS. Show the resolved test
  configuration and actionable unsupported prerequisites. Never switch provider
  or credentials, skip an incompatible case, or weaken assertions to claim
  default coverage.
- [x] Start from the proven Responses reasoning/Studio and Completions GLM5
  workflow profiles. A single universally capable application model is not a
  prerequisite. Qualify effective test settings, calibrated fingerprints,
  isolation, usage/cost attribution and launcher execution after integration.
- [x] Distinguish an unset or unsupported test profile from a stale supported
  calibration. Offer calibration only after an eligible model/API/capability
  selection exists; do not imply that a pilot adds missing structured support.

The earlier default-Responses calibration request exposed this gap. That
checkpoint had no qualified strict-schema workflow selection for OSS20B Responses. A
fresh 1024-token prerequisite request with the default reasoning control
produced no response within the workflow's 120-second call budget; the
diagnostic was stopped later with usage unknown. No pilot ran or baseline was
created. This does not prove upstream support is impossible, and implementing
native Responses workflow support is not required if isolated test profiles
provide the already-qualified Completions route. Existing matrix passes remain
configuration-bound historical evidence.

## Isolated test-profile delivery

- [x] Complete [operation-scoped provider test profiles](implementation_plan_f4l3.md) and merged-source qualification. Historical explicit-profile passes above do not establish this defaults-only acceptance.

Current operation defaults also select native GLM5 Completions for the two
tool-bearing cases. Fresh independently reviewed shared-fixture qualifications
provide Bedrock cap 52 and GPT cap 48 under their separate fingerprints. The
complete normal catalog passed: 17 core cases plus one provider UI case, Allure
publication and clean exit. Unit coverage is exact across 45,926 statements and
17,992 branches (18,674 tests). Final package/external review passed; human
acceptance remains open. Earlier 14+1 passes above retain their original scope.

**Deferred human review**

Substantive work is complete in the linked children; their explicit human acceptance remains unchecked and does not block subsequent work.

- [Operation-scoped profiles](implementation_plan_f4l3.md)
