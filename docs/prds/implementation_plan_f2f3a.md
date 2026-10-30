# Deferred reasoning-comparison stability

[Reasoning comparison](implementation_plan_f2f3.md)

This is future work, ordered after the existing final profiling, authentication and test-domain evaluation tasks. The observed reasoning-token comparison is stochastic: a successful sample does not establish stable separation between effort settings. Its strict score remains unchanged while this work is deferred. A reported human failure remains a failure; deferral is not a passing result or human acceptance.

- [ ] Investigate variability in the existing three-pair comparison after the earlier implementation order is complete. Preserve every failed and successful observation, effective request settings, model/revision attribution when available, nonce identity and execution concurrency.
- [ ] Distinguish client routing/usage defects from upstream response variability. Keep deterministic native-effort and signed-scoring regression controls regardless of the empirical test's eventual disposition.
- [ ] Propose a bounded stability strategy with evidence, cost and runtime tradeoffs before changing the prompt, pair count, thresholds, retry policy or pass/fail contract. Do not hide replacement requests, discard unfavorable pairs or tune until green. Obtain approval for any changed acceptance rule and any new live spending not already authorized.
- [ ] Independently review the eventual design and implementation, verify appropriate lifecycle/accounting controls and complete the applicable delivery gates. Record human acceptance separately.

No reliability guarantee, weakened assertion, automatic rerun or new qualification campaign is introduced by this planning entry.

- [ ] Investigate successful upstream responses with token usage but no visible answer separately from effort-delta failures. Preserve the empty result and usage; reject it as an invalid measurement without discarding pairs or silently repeating inference. Improve diagnostic specificity under the eventual reviewed stability plan.

## Visible-answer diagnostic

- [x] Evaluate one bounded prompt clarification requiring the already-requested solution in the final answer. Preserve the puzzle, six real calls, effort selection, validity checks, output budget and signed thresholds. Keep prior failures; do not replace invalid calls or claim a stability qualification from one successful probe. Retain a prompt change only after independent review and actual measurement.

- [x] Emit fixed diagnostic categories distinguishing missing visible answers, absent/invalid reasoning counters, response/model failures and output-budget exhaustion. Preserve the original validity predicate, every observation and the signed-score acceptance rule. Verify public failures without exporting response content.

## Recurrent empty answer — October 29, 2026

The user approved [opt-in reasoning and structured inference evidence](implementation_plan_f2f3d.md). This improves diagnosis without changing measurement validity or claiming an upstream correction. Previously retained payloads were privacy-filtered SDK projections: `output=[]` there does not establish that the raw upstream output array was empty.

- [ ] Resolve the reported recurring Bedrock Responses empty-answer failure.
  Retained provider evidence reports `completed`, `output=[]` and 309 output
  tokens against a 16,000-token cap. The SDK projection cannot distinguish
  filtered reasoning from absent upstream output. Do not reinterpret reasoning as an answer.
- [x] Add pair, requested effort, outcome and token-budget context to the local
  invalid-measurement error without exposing generated text or adding retries.
- [x] Preserve the owned application API's allowlisted failure category, HTTP
  status, pair and effort in HTTP failures, without reflecting its raw response
  body. A real terminal network failure must remain distinguishable from a
  completed response with no visible answer. Preserve all six original calls.
- [ ] Qualify an upstream correction or an explicitly approved alternative
  route/task strategy; preserve existing failed observations. Two unchanged
  six-call Responses runs passed on October 29 (total estimated $0.0162429).
  These are intermittent observations, not a repair or reliability guarantee.
