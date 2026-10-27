# Reasoning deadlines and concurrent qualification

[Reasoning and integration requirements](implementation_plan_f4l.md)

The larger 16000-token low/high comparison exposed SDK timeouts and queueing in
a direct test harness. Repair both without changing the model, puzzle, three
pairs, signed threshold, nonempty replies or below-cap qualification rules.

- [x] Give Bedrock inference a discoverable bounded timeout setting, default
  900 seconds. Pass the operation's remaining budget into the isolated SDK
  worker and maintained client; retain no retries and a short connection limit.
  Validation, authentication and transport work consume the same total budget.
  Shorter parent deadlines must win. Keep setup/catalog budgets separate.
- [x] Give the generic reasoning test a validated timeout contribution, default
  150 seconds and 960 for the bundled Bedrock profile. Propagate it through the
  actual application server, HTTP client and comparison component. The provider
  retains its separate 900-second inference ceiling with room for surrounding
  work within the bundled 960-second setup transport budget.
- [x] Configure the generic integration setup-request budget, default 300
  seconds and 960 for Bedrock, on the actual backend/server and remote clients.
  Validate before resource allocation and honor shorter parents.
- [x] Run the qualification harness through the ordinary setup backend and
  remote per-call leases. Prove six requests reach the provider concurrently
  without serializing behind one synchronous owner. Preserve ownership,
  cancellation, retained cleanup receipts, one authentication identity and
  normal cost/usage reporting.
- [x] Focused offline tests must cover timeout propagation, invalid values,
  real maintained-client configuration, shorter parent deadlines, six-call
  concurrency and cleanup. Require exact line/branch coverage for new and
  changed code, causal controls and distinct adversarial review.
- [x] Repeat the six-call OSS120B Responses low/medium comparison at
  16000 output tokens until a full valid batch passes. Each batch has six
  admissions, no request retries and an immutable receipt. Preserve all failed
  batches and distinguish client/application deadlines from upstream failures.
  The user authorizes this iteration; a passing batch is a smoke qualification,
  not a claim that the stochastic behavior is always stable.
- [x] Reconcile owning requirements, package/source identity, public-boundary
  checks and external PR-style review before the next human checkpoint.

Provider sampling sources change in this repair. Keep earlier workflow pilot
receipts unchanged and do not silently replace their source fingerprints. Check
baseline applicability explicitly; a stale GLM workflow baseline remains a
separate calibration gate from this six-call reasoning comparison. Main
integration and human launcher acceptance are not implied by offline approval.

The first repaired batch demonstrated true concurrency: all three low replies
completed in 3–5 seconds and all three high requests hit the 240-second provider
deadline. This is an independently preserved failed batch, not a passed contrast.
The next iteration uses 900 seconds for inference and 960 for the application
and shared setup transport; it removes the fixed 300-second IPC ceiling without
adding retries or extending shorter parent deadlines.

Two subsequent unchanged batches used the reviewed 900/960-second budgets and
normal concurrent leases. Each returned all six responses without a local
timeout. All six high-effort observations across these batches reached exactly
16000 output tokens, reported 15998 reasoning tokens, and had no visible answer;
all six low replies were nonempty and below cap. The unchanged conservative
measurement rules rejected both batches. Neither is a passing comparison.
Independent offline SDK serialization and decoder checks found no identified
local defect behind that pattern. Model choice remains a substantive open
question; do not weaken the test or claim that longer deadlines guarantee
usable high-effort answers.

The user then selected OSS120B low/medium at the same16000-token cap. This is the
new candidate profile, with the identical puzzle, signed threshold and complete
three-pair validity requirements. Ordinary inference stays OSS20B, and Studio
keeps its existing OSS20B/OSS120B low/high selections. Record SDK response shape
and usage only, with no raw text, reasoning, identifiers or credentials, to
check whether any missing visible answer precedes local decoding. This user
choice does not turn the earlier OSS20B failures into successes.

Include the verified regional Mantle Standard list-price estimate for the new
OSS120B test candidate, using the public offer's input/output rates and the same
bounded inclusive-input interpretation as OSS20B. Keep unknown regions, tiers,
models and actual billed charges explicit; no billing API permission is needed.
This preserves cost reporting when choosing the new test model.

The selected profile passed a complete unchanged batch: all six nonempty replies
were below 16,000 output tokens, with medium-minus-low reasoning differences
1,162, 1,112 and 400. Calls took 6–18 seconds and no local timeout occurred. Its first
batch failed on one SDK-empty low reply; that failure is preserved alongside
the earlier three deadline/cap batches. This follow-up used 30 admissions and
0.42 USD conservative reserve in total, not reported AWS charges. Inference
stopped after the pass. The prior GLM receipt is stale across eight sampling
sources and requires separate fresh calibration; no baseline was rebased.

Fresh focused coverage is 100% across 672 statements/232 branches in six runtime
modules. Shared setup changes have exact changed-code coverage. One cleanup
probe race was retained as a failed run, then repaired only in its test helper:
ENOENT/ESRCH mean exited, running helpers still fail and other read errors
propagate. The SDK's 85 focused tests and six deterministic liveness cases pass,
with distinct high review and a killed regression mutant. External review of
the final source/package and all outcomes approved the scoped repair. It raised
one help-text inconsistency, repaired with five configuration-review assertions
and distinct high review; an independent 30-case recheck closed the finding.
Only five help strings changed; defaults and runtime code stayed unchanged.
The public-boundary and installed/relocated package checks were renewed.

Human launcher acceptance remains in the parent requirements. The stale GLM
workflow baseline requires separate fresh reviewed calibration; this reasoning
smoke pass and scoped external approval do not satisfy that gate or main adoption.
