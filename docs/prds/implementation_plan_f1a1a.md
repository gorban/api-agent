# Bounded prompted-JSON envelope decoding

[Declarative output modes](implementation_plan_f1a1.md)

## Requirements

- [x] Extend the shared JSON parser with opt-in Markdown-fence envelopes, strict
  by default. Parse original JSON first; only a JSON syntax failure may trigger
  unwrapping. Accept one closed whole-response backtick block labelled `json` or
  unlabelled, with matching opening/closing widths of at least three. Surrounding
  whitespace is allowed; prose, multiple blocks, other labels, tildes, unmatched
  fences and incomplete blocks reject. This is a narrow envelope convention,
  not general recursive Markdown rendering.
- [x] Recursively apply the same parser after each unwrap. Nested fence widths
  must strictly decrease; allow at most four envelopes and retain existing
  encoded-size, instance-depth, finite-value and duplicate-key bounds. Do not
  modify strings inside JSON, repair values or relax the original schema.
- [x] Enable envelopes only for actual recorded `prompted_json` generation,
  including explicitly permitted fallback. Native JSON/structured modes, legacy
  structured requests, tool arguments, schemas, credentials and IPC framing remain
  strict. Empty or reasoning-only answers remain terminal invalid responses.
- [x] Use the same parser policy in synchronous/asynchronous validation, IPC
  admission, provider safety checks and bundled JSON consumers. Preserve raw
  provider text, capture, usage and mode evidence unchanged; expose decoded
  values through a separate helper rather than rewriting the returned content.
  Keep formatting instructions concise, with no repeated fence prohibition.
- [x] Verify accepted and rejected envelopes, causal limit controls, original
  schema enforcement, actual-mode isolation, consumer consistency and unchanged
  raw evidence with deterministic tests and external source review.

## Verification

The joined scope passes 1,124 tests with exact 100% line and branch coverage of
shared rich inference, shared response preparation and GPT rich validation
(789 statements, 392 branches). Controlled GPT and Bedrock transports preserve
fenced raw captures; the complete useful-demo test consumes a nested prompted
planner answer alongside an unchanged native reviewer. Five process-local
mutations causally verify decreasing widths, the four-layer cap, syntax-only
fallback, actual-mode isolation and original-schema enforcement. External review
reports no actionable findings and independently passes 291 focused tests. No
mutation remains, no additional live call was made and saved settings are unchanged.

## Acceptance

- [ ] Human reviews the resulting behavior. No live-call campaign, native-mode
  downgrade, hidden retry or schema relaxation is authorized by this change.
