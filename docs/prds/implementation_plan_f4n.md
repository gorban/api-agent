# Authentication prompts and setup deadlines

[Bedrock provider](implementation_plan_f4.md)

[Title context and attended terminal sessions](implementation_plan_f4n1.md)
extend the original presentation-only scope below following user approval.

- [x] Keep every active terminal authentication conversation visible, including
  password-manager unlock, provider login URLs and authorization-code entry.
  Defer ordinary progress, usage and diagnostic output until the conversation
  closes; preserve ordering and complete scrubbed diagnostics without buffering
  credentials or authentication transcripts in the report/output queue.
- [x] Bound memory of the deferred scrubbed-output queue and release its storage on
  success, failure and cancellation. Preserve terminal echo restoration and
  the existing interaction ownership/cleanup contract.
- [x] Serialize simultaneous authentication channels without blocking IPC open;
  queue later prompts privately, allow input only to the visible conversation,
  and keep terminal echo masked until the last channel closes.
- [x] Preserve login policy: terminal presentation must not enable a provider
  sign-in forbidden by NO_STD_IN, change indexed identities or substitute an
  attended login for a selected unattended account.
- [x] Report authentication and provider-operation deadlines explicitly instead
  of normalizing TimeoutError to an uninformative unknown failure. Do not infer
  a network error from a local deadline or export arbitrary exception text.
- [x] Verify shared provider and password-manager interactions, launcher wiring,
  terminal ordering, cleanup and credential-safe wire diagnostics.
- [x] Independent review closes the concurrent-input/echo, premature replay and
  failed-output spool cleanup findings.
- [ ] Human acceptance with the selected real account and attended flows.

The reported 11/19 run failed shared setup after approximately 60 seconds.
Its retained report contains only `OperationalError: unknown`; it cannot prove
whether the deadline was spent in credential recovery or a provider request.
That loss of classification is a defect, not evidence of a Mantle API-key
configuration incompatibility.

## Verification

548 focused units pass. Terminal interaction and deferred-output modules have
100% line/branch coverage; the new setup/refresh/provider deadline branches are
covered, including safe IPC round-trips. Mutations disabling the output hold or
deadline recognition fail the corresponding regressions. Real local PTYs verify
echo restoration in both overlapping-channel close orders. Large output and
failed write/flush tests verify spool delivery and disposal.

No paid calls were produced for this repair. The user subsequently reports 19/19
passing in API-key mode with NO_STD_IN; session mode remains separately open.
The earlier 11/19 result remains a retained failed observation; the
specific expired backend stage cannot be identified from its old diagnostic.
Full-project unit/coverage and publication gates are not claimed by the focused
run. The existing raw partial-line assembly is unchanged; the memory bound here
applies to the deferred already-scrubbed queue, not a new streaming scrubber.

## Deferred human review

The prompt/deadline repair and attended-session extension are implemented and
independently reviewed. The user accepts the API-key run at 19/19; attended
session login remains a separate human checkpoint in the child.

- [Terminal context and session handoff](implementation_plan_f4n1.md)
