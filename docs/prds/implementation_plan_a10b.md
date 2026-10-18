# Complete credential scrubbing and cleanup causes

[Failure diagnostics](implementation_plan_a10a.md)

## Requirements

- [x] Exact and normalized registered values share compatible detection views when secrets contain genuine controls or alternate/nested JSON escapes. Protect partial and complete captures on both streams, including UTF-8 byte and UTF-16 JSON-escape cutoffs, retaining functional output and disposal.
- [x] Capture decoded registered source spans before syntax rewriting and carry those original spans through every replacement. Encoded composite, header and URI credentials must not leave fragments after their inner fields are masked. Reuse one shared range-tracking path rather than independent pre/post masking guards.
- [x] Incomplete capture at an output bound, deadline, cancellation or cleanup failure must not export a trailing prefix of a configured or registered secret. Cover both streams, short reads, Unicode byte boundaries, invisible insertions, nested escaped carriers and overlapping values. Map detection ranges to original bytes; preserve functional stdout and owned disposal. Recognition must remain bounded, fail closed at its decoding depth limit and avoid quadratic rescanning.
- [x] Scrub complete multipart Authorization credentials, including Digest, opaque multipart schemes and structured quoted headers, while retaining useful noncredential context.
- [x] Scrub configured and registered secrets in complete shell arguments, including concatenated apostrophe quoting, comma/newline values and incomplete arguments at an explicit command truncation marker. Owned command evidence scrubs complete structured credential arguments before quoting and truncation. Preserve noncredential arguments.
- [x] Secondary prerequisite cleanup failures, including BaseExceptions before disposal completes and failures during combined retry, preserve an existing primary interruption and its exit status. If disposal causes the first interruption, retain that interruption with prior ordinary failure evidence and pending disposal ownership; preserve complete local command evidence and retry ownership. Retain all direct and attached cleanup receipt chains from both the command failure and the disposal failure, deduplicating receipt/owner identities and tolerating cycles. Retain successor receipts from every pending callback. A failed outer duty that rethrows itself remains pending even when its attached child settles; only a genuine replacement chain omitting the original duty may supersede it. Settle each successful duty once; retry only pending duties. Fully completed retries are idempotent, and a composite receipt never adopts itself as a successor.
- [x] Inspection end/abort failures retain complete exception chains on pending receipts and primary interruptions. Merge prior causes without duplicate identities or self-references. Preserve retry and disposal behavior.
- [x] Actual noninteractive stderr proofs with LOG_CONTENT=False and focused regression mutations verify these contracts. Safe runner events and persisted reports remain unchanged.

- [x] Recognizable raw credential `KEY=value` command arguments redact the complete value before quoting or truncation, including commas, newlines, apostrophes and long values; unrelated assignments remain useful.
- [x] Confirmed process disposal is acknowledged at receipt creation, before retry, and after interrupted drained offloads. Never finalize an acknowledged handle again; serialize the entire receipt retry, including independent duties, and retain ownership through cancellation and drained finalization. Settle acknowledged release callbacks once; retain original interruptions and evidence.

- [x] Retain raw byte detection and a truly printable detection view when malformed UTF-8 follows a captured registered-secret prefix. Protect stdout and stderr at output limits, deadlines, cancellation and cleanup; preserve unrelated malformed output and functional process results.

## Acceptance

- [x] Applicable source unit checks pass with complete line and branch coverage for the renewed repair candidate.
- [x] Renewed independent adversarial recheck closes the ownership, span-mapping and shared linear matcher repairs.
- [x] The final intended public-boundary audit closes for the complete candidate.
- [ ] Human acceptance of terminal failure diagnostics remains pending.

## Verification

[Detailed capture and disposal evidence](implementation_plan_a10b1.md) preserves prior checkpoints and their scope. Current repairs require renewed controlled-process, mutation, coverage and installed-package verification.

Stored cleanup-handle progress and kernel lease acknowledgement are specified in
[stored cleanup receipt progress](implementation_plan_a10b2.md).
