# B2b — Permanent contrived integration fixture

## Requirements

- [x] B2.6 Preserve a contrived two-component fixture. Each component has an LLM
  producer, deterministic format check and semantic guard. The model chooses
  skip/next_a/next_b on first pass by interpreting the supplied random trial UUID:
  its final hex digit maps 0–4 to skip, 5–9 to next_a and a–f to next_b
  (5/16, 5/16, 6/16, approximating thirds). Initial readiness uses the second-last
  digit for the first component and third-last for the second: 0–7 means true,
  8–f means false. Separate random seed digits target independent 50% rejection.
  Python supplies only a fresh UUID per graph; the model emits the actual fields.
  Deterministic checks neither manufacture choices nor repair model output.
  Measure actual route/readiness frequencies; prompt compliance is not assumed. Skip selects the first accepted final output; continuation
  exposes the first accepted output to the second producer. Original user input
  flows verbatim to every producer/check, including repairs. The complete first
  user message is the input source; the final user message contains only machine
  metadata. Neither metadata nor upstream fields replace that original request.
  Repairs preserve
  original route/input; there is no last-pass relaxation. Format defects are fatal;
  expected semantic guard rejection is a normal retry.
- [x] B2.7 Select calibration separately from coverage. Pilot at most 60 graphs,
  stop immediately on any defect, record nodes/routes/retries and failures.
  Derive simultaneous one-sided 95% exact Clopper–Pearson lower bounds using
  alpha=0.05/declared-node-count and sum independent missing-node probabilities
  for a union bound <=0.001. Maximum acceptance cap is 60; stop immediately
  once every declared node successfully executes. Fail at cap; never rerun/skip.
  Independent graph trials assume fresh random UUID seeds and unchanged settings;
  model compliance and independence are assumptions, not proven properties.
- [x] Missing, stale, sparse or invalid calibration never starts an implicit
  pilot. Bind evidence to model, effective settings and source hashes; recompute
  all statistical claims from contiguous per-trial records and require route/node
  consistency with the exact declared node set. Verify a local integrity receipt
  before accepting records. Reserve the destination before provider setup; store new
  calibration atomically and exclusively without replacing existing evidence.
  A separate owner-only receipt key detects edits or detached JSON; it does not
  defend against an adversary controlling the same OS account. Bind backend
  identity without exposing endpoints, auth mode, driver/input and provider code.
- [x] B2.11 Publish structural integration transitions, attempts, guard outcomes,
  timings, coverage/stopping and usage through the existing Allure/privacy seam.
  Drop producer content and feedback. Unit tests stay outside Allure.
- [x] Verify installed artifacts and document prerequisites, commands,
  calibration and requirement-to-test coverage.

## Calibration maintenance follow-up

- [x] B2.12 Replace the separately selected calibration integration test with explicit maintenance and reviewed, checked-in model-specific bounds. Normal coverage remains mandatory and needs no local pilot artifact. See [maintenance requirements and acceptance](implementation_plan_b2b1.md). The earlier B2.7 delivery and native pilot below are historical evidence; their test-selection procedure is superseded by this follow-up.

## Implementation evidence

`tests/unit/test_workflow_contrived.py` covers strict duplicate/nonfinite JSON,
verbatim checks, immutable repair routes, independent upstream payload and strict
last-pass guards. `test_workflow_sampling.py` covers exact bounds, stale/tampered
claims, sparse declarations and early stop/cap. `test_workflow_evidence.py`
covers structural evidence and omission of arbitrary content. These three modules
passed 76 focused tests with 100% combined line/branch coverage on 2026-10-12.

At the initial phase-one checkpoint, `tests/integration/test_workflow.py` declared separate pilot and coverage tests. B2.12 supersedes that collection contract: the module now declares only mandatory node coverage, and calibration is an explicit maintenance command. The earlier live measurements below remain historical evidence. Human review of the declaration, native traces and integration interface remains open.

## Acceptance

- [x] Focused mutations of duplicate-key and recomputed-claim rejection fail their tests.
- [x] Independent adversarial review complete.
- [x] Combined applicable unit suite passes 100% line/branch coverage.
- [x] Installed artifact and evidence consumer verification complete.
- [x] Explicit separately selected live pilot accepted; retain run identifiers,
  node hits, route/retry frequencies, usage and duration evidence.
- [x] Explicit separately selected live coverage accepted; retain stopping reason.

The probability bound concerns missing-node coverage, not overall live-test pass
rate. Model sampling frequencies remain observed measurements.

### Typed timeout evidence

The permanent fixture binds GRAPH, both components and all six nodes to typed
shared demo configuration declarations. LLM inference originally defaulted to 60 seconds; the current default is 120 seconds;
optional graph/component/node limits remain unset. Resolved settings flow through
pilot and coverage and their fingerprints. Tests verify default/environment/CLI
resolution, nested clipping and that swallowed LLM cancellation cannot return
success after its deadline; the post-call deadline mutation is rejected.

The final deterministic gate passed 5,740 units plus 27 subtests. Combined coverage
covered every one of 17,732 source statements and 6,666 branches. A real synthetic
pytest/IPC/HTTP recording proof preserved expected graph failures beneath passing
tests and committed its storage manifest after the attachments.

## Native acceptance measurement

The revised fixture passed a 20-graph pilot in the selected GPT subscription
Responses mode: six skip routes, two next_a and twelve next_b, 48 inference
calls and fourteen guard rejections across 34 initial component passes. These
are observed frequencies, not a guarantee of exact uniformity. First-component
node hits were 20/20; second-component node hits were 14/20.

Exact simultaneous 95% lower bounds were 0.7871199821 and 0.4098361520. The
smallest accepted cap is sixteen graphs: missing-node union bound 0.0006496552
(fifteen gives 0.0011008050). Separate coverage passed after one graph and
stopped on all six nodes. Both native runs published recorded evidence.
Independent review recomputed these statistical claims. The bounds retain
the stated independence and unchanged-settings assumptions.

- [ ] Human review of grammar, diagram, native evidence and execution interface.
