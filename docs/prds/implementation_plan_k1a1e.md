# Terminal failure classification

[Complete-operation trace checkpoint](implementation_plan_k1a1a.md)
[Integration trace ownership](implementation_plan_k1a1b.md)

## Requirements

- Classify terminal failures from admitted exception contracts, not exception
  prose, stacks, generated text, or model names. A local workflow failure must
  not become a provider failure merely because it reaches an HTTP boundary.
- Use bounded normalized cause and diagnostic code vocabularies independently
  of content permissions. Credential-bearing messages, arbitrary provider codes,
  exception class names and stacks are not structural attributes.
- Preserve failure classification on failed node, component and graph scopes.
  Report final request failure only when top-level graph execution escapes; a rejected
  candidate or nested graph failure that is handled does not make the completed
  outer graph/request fail.
- Retain a typed failure-origin receipt only at a trusted operation boundary.
  Verify private receipt issuance and exact exception identity; public receipt
  values and carriers copied onto another exception grant no authority. Each
  actual provider failure boundary renews classification and clears any prior
  call UUID unless the current accounting boundary supplies it. Weak receipt
  ownership must not retain exceptions, completed task owners or tracebacks
  after release. Concurrent reused exceptions select the exact task/thread
  receipt; cross-task propagation requires an explicitly common logical call
  token and unambiguous classification/call identity. Without a logical call
  scope, separately issued owners remain distinct even if only one has an ID. Bound per-error ownership and omit correlation permanently on
  overflow. Re-enroll the actual accounted call in its returning caller.
  Provider-owned normalized operational failures are provider failures; the same
  exception without that source authority remains unclassified execution failure.
  Only an actual canonical accounting receipt may supply the failed logical call
  UUID. Preserve it through deliberate error wrappers to node, graph and request;
  do not substitute transport, projection, request or guessed identities. Missing,
  unadmitted or ambiguous accounting receipts leave correlation unavailable.
- Preserve explicit selected-runtime ownership, cancellation and deadline
  semantics. An absent or mismatched runtime cannot adopt another request.
- Keep detailed failure content and rejected candidates behind the existing
  destination-specific content and live credential boundary. No diagnostic
  projection grants retry, changes budgets or executes a second operation.

Fixed diagnostic codes are `schema_validation`, `candidate_validation`,
`contract_validation`, `workflow`, `provider`, `deadline`, `cancelled` and
`execution`. Ordinary `ValueError`/`UnicodeError` do not establish a candidate
contract failure. Optional receipt metadata uses one stable read and cannot
replace an exception that refuses metadata reads or writes.

## Acceptance

- [x] Prove normalized local/provider/validation/deadline/cancellation causes
  and fixed codes without sensitive prose or forged arbitrary attributes.
- [x] Prove both parsed executors propagate failed scopes and request outcome;
  prove repaired execution leaves failed visits but a successful graph/request.
- [x] Prove Studio preserves local workflow classification and public error
  response policy; preserve selected-runtime and late-task rejection controls.
- [x] Run focused causal controls, independent review, exact coverage and
  complete joined verification before accepting the repair.

- [ ] Human acceptance confirms terminal causes and repaired outcomes in the selected trace viewer.
