# Direct API reasoning-effort comparison

[Reasoning and model identity](implementation_plan_f2f.md)

## Current accepted behavior

- [x] Exercise the real application HTTP API using GPT-6 Luna through the included official subscription Responses route. One ordinary test uses three matched low/medium pairs, at most six inference requests. This supersedes the former exactly-two-request, 99.9%-reliability qualification requirement; preserve earlier failed evidence without presenting it as qualification of this design.
- [x] Keep the puzzle and all request settings identical within each pair except reasoning effort. Prefix a unique non-secret run marker at the beginning of each pair's prompt, reusing that exact marker for low and medium. Use a new marker between pairs and test invocations. Preserve marker identity in private test evidence; do not vary the task between efforts.
- [x] Require all measured responses to complete. Response-text correctness is not an acceptance criterion: arbitrary or wrong answers do not gate the reasoning-token comparison. Validate reported reasoning counts as nonnegative integers; missing usage is unknown and fails measurement, never zero.
- [x] Compute each signed difference as medium reasoning tokens minus low reasoning tokens. Pass only when at least two of the three differences are at least 50 tokens, and no difference is less than or equal to -50 tokens. A third near-zero or modestly negative difference is allowed. Do not use absolute differences, discard an unfavorable measured pair, or run replacement attempts until green.
- [x] Use an independently authored constrained puzzle. Perform bounded development to observe useful effort separation; do not add an answer oracle to this token-only measurement. Select and freeze the prompt before ordinary delivery. This is an empirical smoke-test threshold, not a promised provider behavior or a 99.9% population-reliability claim. No 2,995-pair campaign is required.

## Attribution, lifecycle and failures

- [x] Record each attempted call, its pair and effort identity, requested/actual model and reported serving revision, original usage and applicable accounting. Preserve suite/test/per-call attribution and error-spend recording. Unknown revision or monetary charge remains unknown.
- [x] Reject unsupported model/route/settings before inference. Keep provider authentication, request and shared execution deadlines, cancellation, cleanup ownership and exclusive suite-use behavior. Transport/authentication/invalid-response failures fail the test rather than triggering a new pair. Do not change existing timeouts to make the comparison pass.
- [x] Preserve historical development and failed fixed-qualification records outside public resources. Do not install the old null qualification asset as a reason for this revised ordinary test to fail before execution. Retire or defer unneeded calibration-only machinery explicitly rather than requiring it for the normal integration path.

## Meaningful verification and delivery

- [x] Controlled integration checks must derive reported reasoning usage from the native effort actually received, never expected labels or call order. Swap low/medium on the sending path while preserving labels and scoring assertions: the signed difference must become negative and reject the test. Force both requests to one effort and verify zero-difference rejection. Restore/remove mutations without requiring a second paid successful run.
- [x] Test two/three qualifying pairs, a near-zero third, exact positive/negative 50-token boundaries, a strongly negative third, response-text irrelevance, missing usage, nonce equality within pairs and inequality between pairs, ignored/swapped routing, and no hidden replacement calls. Verify partial/error accounting and disposal.
- [x] Require applicable exact unit line/branch coverage, controlled HTTP/browser checks where relevant, installed-package behavior, licenses and public tree/index/metadata checks, internal independent review and a scoped commit. External PR review and human acceptance remain separate. No result from the old two-call predicate certifies the new three-pair acceptance rule.

The ordinary runtime now uses the existing provider fixture and shared application HTTP helper, with three matched pairs and the signed aggregate rule above. It records observations through standard test properties while the ordinary fixture retains authentication, accounting and disposal ownership. The earlier direct-answer constraint puzzle is retained as historical evidence below; the code-generation revision changes the task while preserving the comparison contract. Earlier development attempts failed their then-required exact-answer validation and remain historical evidence; the current acceptance rule intentionally measures reasoning-token differences without answer validation. Controlled checks cover scoring, nonce isolation, native effort routing and partial error usage. Calibration-only actors, maintenance CLI and qualification assets have been retired.

The earlier five-variable direct-answer prompt passed one live six-call comparison: low reasoning counts were zero and medium counts were 95, 84 and 89, giving signed gaps +95, +84 and +89. Internal independent review and joined validation are complete. Earlier correctness-gated failures remain historical evidence; they do not qualify the current usage-only rule. This single smoke does not establish population reliability.

- [x] Complete external PR review of the earlier usage-only comparison.
- [ ] Human acceptance of the usage-only comparison remains open.

## Code-generation task revision — October 18, 2026

- [x] Ask the model to produce executable Python code for an independently authored colored-path puzzle. Specify a general DAG solver with exact red-edge count, a minimum blue-edge count, a consecutive-color limit, minimum-cost path counting and a lexicographic optimal witness. Include one fixed example and require code only. Keep the same task and settings for both efforts.
- [x] Retain three matched pairs, the leading shared per-pair marker, fresh markers between pairs and runs, and the signed acceptance rule: at least two gaps >=50 and every gap >-50. Generated code is arbitrary response text: never execute it or validate its answer in this usage-only comparison.
- [x] Controlled checks exercise the actual application sender and native effort body with a conflicting provider default, identical native input/settings within pairs, marker identity, and code-generation task delivery. Historical failing gap vectors remain rejected. These controls establish routing and scoring, not deterministic prompt separation.
- [x] Observe one frozen six-call code-generation smoke and retain every result. A failed run remains failed; no replacement pairs or repeated tuning until green.
- [x] Complete independent review of this revision.
- [x] Complete external PR review of this revision.
- [ ] Human acceptance of this revision remains open.

Later direct-answer observations (+89/+29/+49 and -47/+7/+17) failed the unchanged rule. A larger direct-answer permutation task (+112/-94/-214) and an algorithm-audit task (-123/+92/+101) also failed. The earlier successful smoke applies only to its original task/run. The code-generation revision is an empirical task change with no statistical reliability guarantee and no claimed provider-mapping defect.

The first frozen code-generation smoke completed all six calls and passed the unchanged signed rule. Low reasoning-token counts were 0, 412 and 0; medium counts were 331, 415 and 404, giving signed gaps +331, +3 and +404. The near-zero second pair was retained. This single successful smoke supports this task/run only; it does not establish population reliability or repair a demonstrated provider-mapping defect. Generated code was neither executed nor answer-validated. Independent and external review are complete; human acceptance remains open.

## Concurrent execution — October 19, 2026

- [x] Issue the same three matched pairs as six concurrent application HTTP requests. Keep prompt, model, settings, marker rules and signed scorer unchanged. Preserve records in pair/effort order even when calls finish out of order.
- [x] Under the ordinary shared backend, give each call an independent transport lease and loop while retaining the same authentication owner and selected identity. Serialize catalog access and standalone fixture fallbacks. Keep this adapter limited to the reasoning integration test.
- [x] Retain receipts for every attempted call, await already-issued siblings after failure, and explicitly cancel/drain on interruption. Retain failed child cleanup and every prior receipt behind retryable owners until acknowledged, preserving interruption identity. If scheduling fails, cancel and drain created tasks and close unsubmitted work. Never replace failed or unfavorable pairs.
- [x] Prove six-way overlap through real loopback application HTTP and shared setup IPC using a synthetic barrier; verify native effort, identity, per-call quote/content attribution and cleanup. No new live inference is required to establish scheduling behavior.

A later human run failed the unchanged scorer with gaps -22, -37 and +41. It remains a failed observation; concurrent scheduling does not establish reliable stochastic effort separation. That separate issue is [deferred to the future reasoning follow-up](implementation_plan_f2f3a.md).

- [x] Complete independent and external review of concurrent scheduling, per-call attribution, cleanup and installed-package behavior.
- [ ] Human acceptance of concurrent scheduling remains open. Stochastic token separation remains deferred separately; a positive token-margin sample is not required to demonstrate concurrency.
