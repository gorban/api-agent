# Direct API reasoning-effort comparison

[Reasoning and model identity](implementation_plan_f2f.md)

## Current accepted behavior

- [x] Exercise the real application HTTP API using GPT-6 Luna through the included official subscription Responses route. One ordinary test uses three matched low/medium pairs, at most six inference requests. This supersedes the former exactly-two-request, 99.9%-reliability qualification requirement; preserve earlier failed evidence without presenting it as qualification of this design.
- [x] Keep the puzzle and all request settings identical within each pair except reasoning effort. Prefix a unique non-secret run marker at the beginning of each pair's prompt, reusing that exact marker for low and medium. Use a new marker between pairs and test invocations. Preserve marker identity in private test evidence; do not vary the task between efforts.
- [x] Require all measured responses to complete. Response-text correctness is not an acceptance criterion: arbitrary or wrong answers do not gate the reasoning-token comparison. Validate reported reasoning counts as nonnegative integers; missing usage is unknown and fails measurement, never zero.
- [x] Compute each signed difference as medium reasoning tokens minus low reasoning tokens. Pass only when at least two of the three differences are at least 50 tokens, and no difference is less than or equal to -50 tokens. A third near-zero or modestly negative difference is allowed. Do not use absolute differences, discard an unfavorable measured pair, or run replacement attempts until green.
- [x] Use a small independently authored constrained puzzle. Perform bounded development to observe useful effort separation; do not add an answer oracle to this token-only measurement. Select and freeze the prompt before ordinary delivery. This is an empirical smoke-test threshold, not a promised provider behavior or a 99.9% population-reliability claim. No 2,995-pair campaign is required.

## Attribution, lifecycle and failures

- [x] Record each attempted call, its pair and effort identity, requested/actual model and reported serving revision, original usage and applicable accounting. Preserve suite/test/per-call attribution and error-spend recording. Unknown revision or monetary charge remains unknown.
- [x] Reject unsupported model/route/settings before inference. Keep provider authentication, request and shared execution deadlines, cancellation, cleanup ownership and exclusive suite-use behavior. Transport/authentication/invalid-response failures fail the test rather than triggering a new pair. Do not change existing timeouts to make the comparison pass.
- [x] Preserve historical development and failed fixed-qualification records outside public resources. Do not install the old null qualification asset as a reason for this revised ordinary test to fail before execution. Retire or defer unneeded calibration-only machinery explicitly rather than requiring it for the normal integration path.

## Meaningful verification and delivery

- [x] Controlled integration checks must derive reported reasoning usage from the native effort actually received, never expected labels or call order. Swap low/medium on the sending path while preserving labels and scoring assertions: the signed difference must become negative and reject the test. Force both requests to one effort and verify zero-difference rejection. Restore/remove mutations without requiring a second paid successful run.
- [x] Test two/three qualifying pairs, a near-zero third, exact positive/negative 50-token boundaries, a strongly negative third, response-text irrelevance, missing usage, nonce equality within pairs and inequality between pairs, ignored/swapped routing, and no hidden replacement calls. Verify partial/error accounting and disposal.
- [x] Require applicable exact unit line/branch coverage, controlled HTTP/browser checks where relevant, installed-package behavior, licenses and public tree/index/metadata checks, internal independent review and a scoped commit. External PR review and human acceptance remain separate. No result from the old two-call predicate certifies the new three-pair acceptance rule.

The ordinary runtime now uses the existing provider fixture and shared application HTTP helper, with three matched pairs and the signed aggregate rule above. It records observations through standard test properties while the ordinary fixture retains authentication, accounting and disposal ownership. The current constraint-code prompt remains unchanged. Earlier development attempts failed their then-required exact-answer validation and remain historical evidence; the current acceptance rule intentionally measures reasoning-token differences without answer validation. Controlled checks cover scoring, nonce isolation, native effort routing and partial error usage. Calibration-only actors, maintenance CLI and qualification assets have been retired.

The frozen prompt passed one live six-call comparison: low reasoning counts were zero and medium counts were 95, 84 and 89, giving signed gaps +95, +84 and +89. Internal independent review and joined validation are complete. Earlier correctness-gated failures remain historical evidence; they do not qualify the current usage-only rule. This single smoke does not establish population reliability.

- [ ] Complete external PR review and human acceptance.
