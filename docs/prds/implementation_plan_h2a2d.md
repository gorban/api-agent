# Managed container visibility and bounded logs

[Shared reporting lifecycle](implementation_plan_h2a2.md)

## Bounded scope

- [x] Inventory running and stopped containers within the currently selected managed stack scope. Verify exact full object identities and ownership from fresh engine inspection; exclude the workbench host, development-tool containers, unrelated ownership and legacy scopes requiring migration. Do not adopt resources merely because a port, name or label appears plausible.
- [x] Keep Container management accessible with zero running containers, including an empty or failed inventory. Show running/stopped state and refresh explicitly. Existing confirmed Stop retains containers and volumes and rechecks active leases and exact ownership at mutation time; only running rows enter stop selection.
- [x] Read logs only after revalidating the highlighted exact object, including stopped objects. Request a finite 200-line engine tail, use the shared bounded process collector and five-second inherited command deadline followed by owned process cleanup acknowledgement, and display at most 64 KiB of sanitized output. Include both output streams without claiming cross-stream chronology. Never follow a stream indefinitely.
- [x] Reuse a scrollable, selectable, copyable read-only log page for future local-run output. Closing or cancelling a page must stop and settle its owned reader before disposal; delayed output must not redraw another screen. Show safe, actionable failure state without raw engine error details.
- [x] Scrub complete bounded captures using the existing credential policy, including configured/environment and explicitly registered secrets, before stream headings or presentation clipping. Use the reviewed engine-transport environment allowlist for inventory, inspection, stop and logs; do not send configuration secrets to the child environment. Preserve partial-credential and escaped-value protection; copied text is the same sanitized text displayed.

Broader registry discovery across other stack scopes, lifecycle administration of external/shared foreign installations, volumes, removal, purge, reporting-wide pages and local-run execution remain deferred. This read-only view does not establish new lifecycle authority or complete those parent requirements.

## Verification and acceptance

- [x] Prove full-ID inspection, stopped and empty inventories, independent ownership/session exclusions, stale/replaced selection, safe command arguments, both-stream output, byte/time bounds, cancellation and process-group disposal with synthetic engines and controlled processes.
- [x] Exercise mounted terminal navigation, row selection, refresh, scrolling, selected/all text copy, zero-running access, close/quit and delayed reader completion. Reject meaningful mutations at the intended ownership, selection, redaction and lifecycle checks.
- [x] Complete exact applicable line/branch coverage, installed-package/license/boundary checks and independent review before delivery.

- [ ] Human acceptance: inspect running/stopped rows, zero-running access, log selection/copy, refresh and Close/Quit with disposable services. Normal Stop retains volumes.

The installed log reader uses the shared bounded command collector and its
self-retaining aggregate cleanup receipt. Alternate collectors must be qualified
for independently transferred successor duties before reuse; this slice does
not promise an unrestricted loader cleanup protocol.

The joined 32-worker unit gate passed 10,884 cases and 27 subtests at exact measured line and branch coverage. Independent guards and installed-package/public-boundary checks passed.
