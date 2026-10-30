# Final command diagnostic rendering

[Unit-suite performance investigation](implementation_plan_a9i.md)

- [x] Build a failed command's diagnostic once after its cleanup attempt, using
  the final available return code, captured bytes and current secret registrations.
  Share the final safe immutable text among the primary error, escaping cleanup
  error and created cleanup diagnostic as applicable. Do not cache raw content or
  add durable diagnostic storage.
- [x] Retain fallback evidence when selector or pipe closure raises, including
  failures after a successful command. Preserve the current cleanup sequencing,
  exception/interruption identity and causes, secondary diagnostics, retained
  owners and composed retry receipts. Initial prelaunch and process-launch
  failures continue to render their available evidence.
- [x] Verify one render for normal failure, closure failure, finalizer failure and
  cancellation, late credential registration, final status and redaction. Preserve
  real process disposal and existing retry/cancellation proofs. Reject a causal
  mutation restoring premature or repeated rendering.
- [x] Compare matched covered response-bound cases and independently review the
  production change. No guard or response-size limit changes are allowed.
- [x] Complete exact applicable line/branch and public-boundary verification; subsequent commit checks remain mandatory.

Ten new regression cases joined existing prerequisite and terminal-diagnostic
checks in a 255-test passing focus run. The causal mutation restoring early
rendering failed before finalization, as required. The correctly covered matched
four-case benchmark passed in 64.55 seconds versus 120.85 before this production
change; this single serial pair does not establish full-suite wall-time savings.

A covered representative profile observed exactly one evidence build after
confirmed disposal, with one full-stream capture and one empty-stream capture.
Bulk credential normalization remains the dominant cost. Further normalization
work requires a separate measured decision and is not implemented by this repair.

Human acceptance of the delivered recent unit repairs and noninteractive full-suite performance is recorded. Broader optimization and interactive picker behavior remain separate.
