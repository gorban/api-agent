# Canonical GRAPH payload storage

[Complete flow-aligned evidence](implementation_plan_b1c1a2.md)

Store permitted node inputs/outputs and actual visible LLM requests/responses once per execution occurrence. Native Allure GRAPH attachments and future workflow consumers use the same files through a validated index. This slice does not deliver the historical overlay UI, complete durable test-log transport, definition deduplication or executed-setting snapshots. Application tracing remains independently controlled; separate test-backend trace export is outside this slice.

## Identity and representation

- [x] Qualify payload identity by recorded run/test and occurrence. Node capture uses invocation/event/direction; LLM capture uses call/phase. Preserve every use-site ownership link. Equal values from different occurrences remain distinct. Repeated delivery of the same occurrence must agree; conflicting content or ownership rejects.
- [x] Capture actual validated provider wire inside its credential-owning boundary. Preserve permitted request settings, visible messages, output, usage and response metadata; exclude authentication, continuation state and hidden reasoning before IPC or retention. Bind transported phases to the active worker node and the authoritative call identity. Keep request evidence when execution fails and preserve partial response evidence when available.
- [x] Preserve the explicit trusted setup-owner evidence extension for custom owners under its existing admission and bounded transport. The built-in backend must use its new credential-owned capture, without rebuilding missing phases from caller request/result objects. A compatibility callback must not override authoritative collected phases.
- [x] Persist flat manifest-hashed JSON payload files with explicit occurrence identity, source, digest, byte length, capture reason and projection links. A version 2 durable journal replaces embedded values with typed payload links while retaining its original version 1 digest. Hydration must rebuild the exact version 1 journal and pass its existing validation. Do not relabel a changed journal as digest-valid. Preserve legacy empty request/response omission projections without an empty payload file or a changed original digest.
- [x] A journal's visible LLM subset resolves through a declared projection of the canonical provider payload. Do not store a second copy of that request/response. Retain legacy histories unchanged and reject ambiguous joins rather than matching text or assuming the latest call.

## Bounds and disclosure

- [x] Preserve nested JSON objects, arrays, scalar types and null. Schema-admitted capture exceeds the former 512-character display preview, within 64 KiB encoded per-payload admission, depth 8, 32 object entries, 16 array entries and 2,048 items. Measure payload admission and validation with the same canonical UTF-8 JSON encoding. Keep the 4 MiB / 8,192-event journal aggregate and the separate actual transport-frame budget. Oversized or unsupported data has explicit incomplete/unavailable evidence; never claim a full capture after a drop. Legacy plain-scalar preview behavior remains readable.
- [x] LOG_CONTENT controls test payloads independently of TRACELOOP_TRACE_CONTENT. Withholding retains structural identities/accounting, without confidential payload files. Credentials remain redacted under every flag combination.
- [x] Attach existing canonical sources beneath the correct graph/component/node visit or call, retaining nested JSON for progressive inspection without inventing timed field steps. Untimed calls stay on their owning node; calls outside workflows remain inspectable. Preserve colors, authoritative outcomes, frozen accounting and partial/missing distinctions.
- [x] Expose a reusable bounded resolver for manifest-verified files and occurrence/projection links. Reject missing files, wrong digests/lengths, traversal, symlinks and ownership conflicts. Anchor local reads to an opened directory descriptor across path changes; explicit close/context management and finalization must release that descriptor safely. Preview and finalized publication include every reachable source; resolution must not consult current workflow files or tracing services.

## Acceptance

- [x] Deterministic separate-backend producer/IPC/finalized-artifact proof covers nested values longer than 512 characters, multiple turns/retries, failure/cancellation, both flags independently, same-source reuse and immutable accounting.
- [x] Legacy and version 2 reopen tests, causal corruption/attribution/privacy controls, exact unit coverage, installed/relocated artifact checks, independent implementation review and external review close all reported issues.
- [x] Human acceptance of GRAPH inputs/outputs and LLM payload inspection. Correct native GRAPH placement and unattached-call inspection were confirmed. No paid inference is required for this deterministic qualification.

## Human review

Canonical payload storage and native rendering are human accepted. Exact unit coverage, relocated packaging and required independent/external source reviews are complete; the broader overlay and durable-log requirements retain their own gates. Conversation grouping and readable turn labels are separate from occurrence-preserving payload storage.
