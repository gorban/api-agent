# Chat request leases and disposal

[Prepared sessions](implementation_plan_i2c.md)

- [x] Isolate operation identities by authenticated backend owner and request session. Bound active operations and retained terminal statuses; reject duplicate identifiers while retained.
- [x] Advertise ten-second heartbeats and expire a request thirty seconds after its last heartbeat. Heartbeats never extend the operation's real deadline. Inject short intervals for controlled verification.
- [x] Make cancel, disconnect, expiry and shutdown drain the actual operation task and its OperationContext. Preserve one terminal result, including usage supplied by the backend. Ordinary cancellation must leave persistent authentication ownership intact.
- [x] Serialize overlapping disposal attempts. Retain retryable cleanup receipts and block admission while unsafe work remains. Publish no completed or cancelled terminal state until cleanup settles.
- [x] Prove caller isolation, heartbeat expiry, immutable deadlines, bounded state, resistant work, overlapping cancellation and cleanup retries with controlled tasks. Complete focused coverage and independent review before acceptance.

This bounded request lifecycle does not provide post-test retained interactive ownership. [Retained Studio requirements](implementation_plan_i2c2.md) remain separate.

Controlled implementation evidence: `api/chat_lifecycle.py` is exercised by 54 dedicated unit tests with 100% line and branch coverage (249 statements, 76 branches). Sixteen focused mutations independently reject broken caller isolation, expiry, late heartbeat revival, pending admission, context drain, cleanup callback ownership, task cancellation, child receipt capture, disposal scope failure handling, receipt deduplication grouped receipt draining replacement receipts raised by disposal callbacks grouped current-duty retention signaling all shutdown siblings before draining, watchdog fail-stop and intentional monitor-stop classification. Cancellation/deadline child receipts survive context completion, and disposal timing scope entry/exit failures retain pending ownership. Scheduler failure, resistant operation tasks and blocked cleanup callbacks retain recoverable ownership. The backend supplies the genuine OperationContext and owns persistent authentication; this engine never disposes that authentication owner. Joined backend validation and independent review pass; human acceptance remains separate.

A mutation permitting overlapping settlement wrappers survives because the retained callback task still serializes resource disposal; repeated idempotent OperationContext closure has no separately demonstrated resource effect. Callback uniqueness and terminal/ownership safety remain asserted.

Disposal callbacks can transfer duties through nested receipt groups. Superseded parent callbacks are not repeated; mixed ordinary failures retain unresolved parent ownership after dispatching named children. Blocked replacement duties prevent terminal publication and admission.

A retry group containing its current pending receipt retains that duty once, after newly transferred children. Nested duplicates do not duplicate callbacks, and previously retired child duties are not replayed. Terminal publication still requires the current duty to acknowledge disposal.

Shutdown signals every active sibling before waiting for any operation to drain. A resistant first resource cannot delay cancellation of a cooperative sibling.

Unexpected watchdog completion, timing failure or interruption fail-stops and settles its operation; observed failures cannot silently disable lease enforcement. Intentional monitor cancellation during successful settlement preserves the original terminal classification.
