# Unrecorded integration usage visibility

[Public workflow checkpoint](implementation_plan_f4e.md)

## Requirements

- [x] The ordinary launcher integration path with reporting skipped captures validated,
  content-free inference usage events in memory and returns normalized suite usage.
- [x] Forward every event once to the existing caller, preserving test status,
  cancellation and worker cleanup. Reject malformed usage instead of silently
  accepting it.
- [x] Validate original usage structure and metadata before privacy export can
  remove forbidden fields or repair invalid values. Reuse the existing accounting
  validator; capture still validates the scrubbed event.
- [x] Scrub usage events before capture and forwarding, and final summaries
  before presentation, using the existing evidence boundary and its registered
  credentials, explicit secrets and configuration-secret handling.
- [x] Send the existing final usage presentation to the caller's usage callback.
  Unknown usage and unknown cost remain unavailable; only validated provider
  quotes contribute cost. Do not fetch prices or substitute another model's rates.
- [x] Keep this path free of report installation, storage acquisition, recording,
  manifests, attachments and publication.
- [x] Independently review the implementation and offline checks.
- [ ] Confirm token usage through the approved attended real workflow checkpoint.

## Implementation

The launcher uses the existing inference event validator and in-memory suite
usage ledger. Its final summary and usage callback share the same report.
Synthetic selected file providers complete through the normal integration
provider and usage publisher on the actual unrecorded launch path without
network access. Final presentation errors retain the execution result and
inspection handles, including failed and cancelled execution and interrupts. This establishes accounting behavior, not real
provider inference access or human acceptance.

Focused checks cover raw validation, completion publication and launcher ownership. All changed launcher statements and branches are
covered. Temporarily removing usage capture or replacing unavailable cost with
zero makes the focused checks fail. Removing the retained result receipt also
fails the presentation-error checks. Bypassing event scrubbing or final-summary scrubbing fails privacy checks.
Bypassing original-event validation fails malformed-event checks whose privacy
export alone would otherwise make the event valid. All mutations were restored.
