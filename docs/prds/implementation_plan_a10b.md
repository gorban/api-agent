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

## Acceptance

- [x] Applicable source unit checks pass with complete line and branch coverage for the renewed repair candidate.
- [x] Renewed independent adversarial recheck closes the ownership, span-mapping and shared linear matcher repairs.
- [x] The final intended public-boundary audit closes for the complete candidate.
- [ ] Human acceptance of terminal failure diagnostics remains pending.

## Verification

[Terminal proofs](../../tests/unit/test_terminal_failure_diagnostics.py) exercise
LOG_CONTENT=False stderr and preserve exact cleanup owners. [Scrubber proofs](../../tests/unit/test_terminal_diagnostics.py)
cover registered values, randomized shell quoting and useful noncredential arguments.
[Prerequisite proofs](../../tests/unit/test_prerequisite_local_diagnostics.py) cover
KeyboardInterrupt, cancellation and GeneratorExit with secondary cleanup and retry.
[Inspection proofs](../../tests/unit/test_inspection_controls.py) retain both cleanup
chains and a primary interruption. Focused mutations restore the old behavior for
each contract and fail the corresponding regression assertions.

Focused modules have complete line and branch coverage. The prior combined gate
passed 6,144 unit cases and 27 subtests, covering all 18,555 statements and 6,964 branches.
Earlier independent review closed its original reproductions; subsequent residuals required renewed review, completed for the delivery below. Human acceptance remains open.

The earlier residual delivery gate passed 6,248 cases and 27 subtests, with exact coverage of all 18,608 statements and 6,998 branches. Real helpers, both receipt chains, cycles, first/second interrupts and self-rethrows retain owned pending duties until their individual acknowledgements. Independent controls and intended-guard mutations close; human acceptance remains open.


A subsequent residual repair scrubs raw credential assignments as complete arguments
and acknowledges process disposal at receipt capture, before process retry and after
a drained interrupted offload. Independent release duties and interruption evidence
remain pending until acknowledged. Its focused synthetic gate passes 306 cases with
all 728 statements and 282 branches covered across diagnostics and prerequisites;
all four intended-guard mutations reject. Both real-helper acknowledgement windows
and actual noninteractive assignment stderr variants pass. Independent adversarial recheck closes these source guards. The complete final-candidate gate passes 6,322 cases and 27 subtests, with exact coverage of 18,621 statements and 7,004 branches. The intended public snapshot and proposed metadata audit pass; installed module hashes match the reviewed source. Human acceptance remains open.

The renewed repair serializes overlapping retries of a single receipt through all
independent callbacks and process finalization, including drained cancellation. Original successor chains survive aggregate diagnostic
attachments, so a cyclic predecessor remains pending until its callback succeeds. A
cancelled queued caller retains the same receipt. Output evidence preserves the
complete bounded capture until configured credentials are scrubbed, then truncates
the safe representation. Read boundaries cannot expose a registered secret suffix;
functional stdout remains unchanged. ANSI CSI boundaries preserve recognizable URI
userinfo redaction without restoring quadratic matching. Unicode tails trim to a
valid UTF-8 boundary; omission counts use emitted diagnostic bytes. Registered
value ranges are recorded against original text and carried through each credential
syntax rewrite. The emitted union masks complete registered composites and separate
secondary credentials even when a registered scheme overlaps their syntax. Complete
registered shell arguments retain their existing redacted representation. Original
registered matches use the generic shared redaction matcher, with a prefix table
and one linear scan per value; overlapping
and adjacent occurrences emit one merged range as each run completes. Dense 8 MiB
output with a 64 KiB registered value retains bounded evidence and verified owned
process cleanup within the existing ten-second synthetic outer deadline. The combined response limit remains 8 MiB,
and each emitted diagnostic stream tail remains at most 16 KiB.
[Focused ownership and output proofs](../../tests/unit/test_prerequisite_retry_output.py)
exercise overlap, cancellation, split reads and both real helper output streams.
The focused renewed gate passes 369 cases with all 835 statements and 326 branches
covered across prerequisites, diagnostics and the shared redaction matcher. Isolated guard-removal mutations
reject overlapping retries, pre-scrub truncation, quadratic URL-token matching,
ANSI URI credential bypass, erased successor provenance, Unicode byte overflow
registered masking that erases credential syntax before recognition, syntax
rewriting that erases complete registered-value matches, missing range mapping,
quadratic overlapping search, dropped overlap suffixes and unmerged dense runs.
Independent overlap probes using the production POSIX finalizer with fake handles
and intercepted signals, plus actual noninteractive stdout/stderr tail probes, pass.
Renewed independent source review closes these guards and the shared matcher. At that source checkpoint, the combined candidate gate and final boundary audit remained open.

[Shared range proofs](../../tests/unit/test_redaction_ranges.py) verify exact
overlapping and adjacent unions, disjoint matches, prefix fallback, Unicode,
empty-value rejection and dense coalescing. Both telemetry and command diagnostics
use the same pure matcher; the diagnostic range mapping remains unchanged.

The renewed source gate passes 6,962 unit cases and 27 subtests with exact coverage of 18,825 statements and 7,092 branches. Joined application verification, final public artifact/index audit and human acceptance remain separate.

Current combined verification passes 7,752 unit cases and 27 subtests, with exact coverage of 20,154 statements and 7,562 branches. Independent adversarial controls close these repairs. Installed wheels match the reviewed runtime, preserve licenses and load from an unrelated working directory. The complete intended public index and neutral metadata pass their audit. Normal selected workflow integrations pass three tests, with nested execution evidence and verified artifact hashes in S3 and secondary storage. Coordinated human acceptance remains open; the separate provider-cache failure is unchanged.

The subsequent capture-edge repair passes 7,875 units and 27 subtests at 32 workers with exact coverage of 20,287 statements and 7,628 branches. [Capture-edge proofs](../../tests/unit/test_credential_capture_edges.py) protect prefixes before UTF-8 decoding and complete escaped values after syntax-aware scrubbing, retaining source-range unions and functional stdout. Independent real-process, IPC, recording and mutation controls pass; the bounded detection decoder rejects excessive nesting. Bulk ordinary-byte mapping keeps normalization linear. The installed wheel matches reviewed source and assets. Earlier failed fixtures and superseded guards remain verification history; human acceptance remains open.

The shared capture path recognizes decoded original intervals before syntax edits, including genuine controls, incomplete escapes and UTF-8 byte and UTF-16 JSON-escape cuts. True 8 MiB cutoff and independent process/mutation controls pass; dense process controls retain the existing 10-second envelope. The extension passes 8,201 units and 27 subtests at exact coverage of 20,634 statements and 7,816 branches. Installed runtime and sampling inputs match. The initial instrumented dense-overlap failures were repaired without increasing their bound. Existing human and cache acceptance remain separate.
