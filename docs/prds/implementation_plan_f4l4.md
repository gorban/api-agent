# Historical reasoning/integration checkpoint

[Current summary](implementation_plan_f4l.md)

Preserved snapshot: statements, checkboxes and measurements describe their earlier scope; current acceptance remains in the summary.

# Provider-selected reasoning and integration qualification

[Bedrock requirements](implementation_plan_f4.md) · [Workflow qualification](implementation_plan_f4b.md)

The user approved reasoning-effort support, short newest-model access/capability probes, Bedrock fixture calibration and provider-contributed model selections for reasoning and Studio tests. Existing workflow provenance checks stay strict; source-checkout config changes become admissible at integration rather than bypassing verification.

- [x] Advertise and encode only model/API-qualified reasoning controls; distinguish an accepted control from observable reasoning-token reporting and from a measured behavioral contrast. Do not label an ignored parameter as supported or invent missing counters. Keep unknown models/routes explicit.
- [x] Replace hardcoded test model/family/version/effort selections with typed provider-contributed variables and generic capability validation. Preserve meaningful GPT regression defaults, precise unsupported prerequisites, actual outgoing request assertions and independent model/effort selection coverage. No provider-specific names or protocol logic in generic test drivers.
- [x] Add Bedrock provider/source/config identity to permanent fixture calibration without accepting GPT bounds for AWS. Preserve exact statistics, contiguous observations, provenance, receipts and independent review. Support required structured requests only on qualified Mantle models/routes; refuse unsupported shapes before inference.
- [x] Run focused offline tests, exact coverage for wholly new files/changed code, causal controls, distinct adversarial reviews and installed/relocated/public-boundary checks before external PR-style review and the next human checkpoint.
- [x] Run the authorized bounded latest-model probes and an explicit initial 20-graph Bedrock pilot with a working model. Keep credentials/raw output private and publish only independently reviewed structural measurements. Stop on unavailable auth or failed pilot; do not cherry-pick observations or silently change model/provider/settings. Additional expansions need explicit approval.
- [x] Qualify a complete live three-pair reasoning comparison under its unchanged signed threshold. Preserve unavailable, empty and cap-reaching samples as failures; partial matched pairs do not establish a passed test. Further inference needs a new bounded authorization after the completed attempt scopes.
- [x] Refresh the stale GLM workflow baseline after the deadline/pricing sampling-source changes, preserving the earlier report and requiring fresh independent review before node-coverage reuse.
- [ ] Human acceptance through launcher reasoning, Studio selection and normal node-coverage tests remains separate from capability discovery and pilot arithmetic.

Completed scoped repair: [reasoning deadlines and concurrent qualification](implementation_plan_f4l1.md).

Current follow-up: [complete Bedrock integration-suite readiness](implementation_plan_f4l2.md).

## Larger reasoning budget follow-up

This checkpoint records the earlier 16,384-token application limit. The
[shared output-ceiling contract](implementation_plan_f1d.md) supersedes that
application limit; qualified test-specific budgets below remain independent.

The user requested four times the original 4096-token comparison allowance.
Raise the generic application and integration-test ceiling to 16384, while keeping
the Bedrock request within its conservatively advertised 16000-token maximum
from AWS's 16K model limit. Restore low/high as the larger-budget candidate;
keep the puzzle, signed threshold, nonempty/cap guards and model-limit preflight.
GPT's unset output default and Studio's effort choices remain unchanged.

- [x] Verify application upper-bound and provider-advertised-limit acceptance
  and rejection with focused tests, exact changed-code coverage, causal checks
  and distinct adversarial review.
- [x] Run one six-request OSS20B Responses low/high comparison, at most 16000
  output tokens each, with no retries and a conservative 0.06 USD reserve ceiling.
  Keep safe SDK error classifications separate from prompts, keys and raw errors.
  Preserve prior failed batches; full live qualification remains open until a
  complete valid comparison passes.
- [x] Reconcile source/package/boundary and external review evidence. The application configuration must match its committed
  workflow provenance. The private validation build freezes that configuration.
  An offline check in the original Python 3.14.7 runtime confirms all ten GLM5
  fingerprint categories, including 57 sampling sources, and the 26-run bound
  remain identical; its existing reviewed receipt remains valid without new
  calibration inference.

The larger-budget focused run passed 353 tests, with exact 100% line/branch
coverage across 251 statements and 90 branches. Distinct high review passed
145 independent cases. The actual loopback application and native encoder tests
cover both accepted limits and rejection before inference; an isolated causal
mutation verifies the application upper-bound regression.

The single six-request 16000-token low/high batch failed. All three low replies
were nonempty and below cap, with 166, 528 and 683 reasoning tokens. Two high
calls produced sanitized SDK APITimeoutError records after approximately 30
seconds; the third application call failed at its 120-second deadline. No high
reply completed, so no matched pair qualified. Six admissions reserved at most
0.06 USD; reservation is not an AWS charge report. No retries, timeout changes,
replacement samples or further inference followed. Aligning SDK/provider and
application deadlines is the next substantive issue; renewed paid testing needs
another bounded scope. The larger output allowance alone did not qualify the
comparison. This direct harness serializes non-remote provider calls; queue time
contributed to the third deadline failure, so it does not prove a third SDK
timeout. Normal remote fixtures use concurrent leases. External PR-style review
approved this scoped cap change after 145 independent focused tests and exact
source/package/boundary checks. Complete live qualification, main adoption and
human acceptance remain open.

## Qualified behavior and measured bounds

[Provider qualification](../../src/api_agent/llm_providers/bedrock/QUALIFICATION.md)
documents the configuration and route matrix. GPT-OSS 20B/120B accept native
low/medium/high controls on both APIs; Responses exposes reasoning-token counts.
GLM 5 Completions qualifies bounded strict schema output. Other schema/effort
combinations remain explicit rejections. Studio selections use actual catalog
family/version metadata, including missing and duplicate version labels.

The initial 20-graph GLM 5 Completions pilot in us-east-1 completed with 35 inference
calls, including three within-graph repair attempts. All 20 graphs hit the first
component; 12 hit the second. Independent receipt, full fingerprint and exact
statistical validation yielded 22 minimum runs plus 4 safety margin: a 26-run cap,
with missing-node bound 0.00015004868382870193. Existing GPT reports are preserved.
The new report binds Python 3.14.7, exact dependency/source hashes, session auth,
default profile and native direct adapter configuration; other settings require
fresh reviewed calibration. Admission reserves are conservative spending caps,
not reported AWS charges.

Source-checkout workflow documents still must match HEAD. The reviewed package
build freezes the changed configuration and validates installed/relocated graph
provenance. Main integration must commit those documents and refresh genuinely
stale GPT baselines; neither requirement is waived by this Bedrock pilot.

Focused offline verification passed 2885 tests plus 283 related lifecycle test
executions. The 19 provider Python files (2351 statements, 814 branches), new
model_choices helper, reasoning contrast and both calibration modules have exact
100% line/branch coverage. Remaining measured shared accounting gaps are on
unchanged code. An additional generic provider-help test encountered existing
shared Git-hook stderr; it is preserved as an environmental failure rather than
included in the clean focused result. Full applicable main units remain required.
Wheel/sdist and installed/relocated offline checks include the actual parsed
application graph, new controls, provider defaults and recalculated baseline
statistics. The existing normal workflow node-coverage test passed through its
ordinary provider and graph boundary, hitting all six nodes in one graph with
two inference calls. This was a direct invocation of the existing test function,
not a new launcher/browser acceptance claim.

The separate six-request OSS20B and OSS120B Responses low/high comparisons both
failed. Each had one unavailable application call and two high replies at the
4096-token cap; the OSS120B high replies contained no visible answer. One OSS120B
low reply was also empty. The final separately approved OSS20B low/medium batch
completed five nonempty, below-cap replies but failed one unavailable medium
call. Its two completed matched pairs increased reasoning tokens by 2282 and
1753. No batch passed all three pairs, and no retries or replacement observations
were used to manufacture a passing score.

The earlier 4096-token candidate Responses profile used OSS20B low/medium with the same puzzle,
4096-token cap and original signed threshold. That historical profile did not qualify. Native low/medium/high support and counter reporting are separate
from this acceptance gate; Studio's short low/high selection profile is unchanged.
Generic measurement validation now requires nonempty normalized replies and
rejects cap-reaching bounded samples even if the provider says completed.
Independent review and causal tests cover these repairs and the external
catalog-feature prerequisite finding. The latest two shared helpers have exact
line/branch coverage. External PR-style review approved the scoped PoC code after
113 independent focused offline tests and source/package verification. This
approval left live reasoning qualification, main integration and human
acceptance open at that stage; the newer follow-up is recorded below.

## Earlier deadline repair outcome

The [deadline follow-up](implementation_plan_f4l1.md) repairs remaining-budget
propagation and concurrent harness leases. The user-selected OSS120B Responses
low/medium 16,000-token profile passed all three unchanged pairs, with reasoning
differences 1,162/1,112/400 and six nonempty below-cap replies. Earlier failed
batches remain failures. This is smoke qualification, not statistical stability.
The new runtime sources invalidate the earlier GLM workflow baseline across
eight source hashes; fresh reviewed calibration was an explicit remaining gate.
The earlier cap-only fingerprint acceptance above is historical. Final external
review approved the scoped deadline/queue repair after its help-text finding
was repaired and independently rechecked. That checkpoint left human acceptance, fresh calibration and main integration
separate; the latest qualification is recorded below.

The [integration readiness follow-up](implementation_plan_f4l2.md) now has a
fresh four-pass GLM5 calibration (20 graphs/35 calls; minimum26 plus margin4,
cap30), a recorded 14/14 Responses pass and recorded 1/1 Completions workflow
pass. Both Allure publications passed; failed earlier runs remain retained.
Final package/source checks and external review passed; human acceptance remains open.

- [ ] Deliver [provider-owned per-test defaults](implementation_plan_f4l2.md) during
  integration. Preserve the earlier qualified matrix and application settings.
