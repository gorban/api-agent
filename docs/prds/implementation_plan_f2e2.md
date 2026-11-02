# Shared parsing, credential scrubbing and diagnostics

[Credential protection](implementation_plan_f2e.md) · [Response boundary contract](../architecture/response_scrubbing.md)

Structured test-profile stdout follows the [typed diagnostic follow-up](implementation_plan_f2e2a.md). Terminal disclosure follows [deferred test streams](implementation_plan_f2e2b.md).

## Requirements

- [x] Managed local development credentials must avoid common-word secrets that collide with ordinary response data. Keep the documented defaults consistent across operation modes; changing initialization defaults does not reset persisted accounts.

- [x] Provide one bounded response boundary accepting an expected schema or existing contract validator, an operation context and the operation credential authority. Return the parsed, scrubbed value with separate structured diagnostics. Preserve safe siblings and admitted extra fields.
- [x] Decode native JSON types without guessing types from string contents. Reject duplicate keys, non-finite numbers, unsupported native objects and exceeded size/depth/work budgets without invoking arbitrary object methods. Keep authentication acquisition, signatures and opaque replay exact; use their existing authenticated owner rather than scrubbing functional credentials.
- [x] Apply credential matching to field names and string values. Omit credential-bearing names rather than renaming them to colliding empty keys. Keep a bounded sidecar sequence for redacted-name diagnostics; no reserved string property or non-JSON key may change an upstream contract. Never include raw field names, values or matched credentials in diagnostics.
- [x] Preserve numeric, boolean and null values through ordinary text scrubbing. Do not invent zero costs, false permissions or empty identities. Redacted strings use a controlled empty value; normal required/minimum-length/schema validation runs at this boundary and rejects unusable fields. Required identities must not be silently changed into another valid identifier.
- [x] Preserve existing registered/encoded/partitioned credential rejection where safe local redaction cannot be established. Reuse the shared matcher and existing registration/publication synchronization; do not duplicate credential normalization in each provider or weaken LOG_CONTENT and TRACELOOP_TRACE_CONTENT.
- [x] Log bounded safe diagnostics centrally at configurable verbosity. Expose the verbosity setting in ordinary configuration review, defaulting to warning. Validation errors explain when scrubbing affected the failing contract without requiring every consumer to inspect diagnostics. Error formatting excludes rejected inputs and secret-bearing keys.
- [x] First prove model discovery with unused metadata containing a registered credential, retained safe model selections, and rejection of unusable identity fields. Verify the actual normal Studio E2E with the selected authentication route.
- [x] Inventory every production parse-and-scrub path, then apply this boundary to each applicable path. Record exact authentication/opaque/streaming exceptions and their protection owners; no silent omission or one-provider-only completion claim.
- [x] Complete exact applicable line/branch coverage, causal rejection controls, installed/relocated resources, public boundary and external PR review. Preserve unrelated work and require human acceptance separately.

## Human acceptance

- [ ] Confirm normal attended GPT Studio E2E discovery, optional tool metadata preservation and useful value-free diagnostics at info/debug verbosity. Authentication and signed continuation channels retain their exact owned contracts.

## Verification

The complete 23,497-case unit run passed with exact 100% line and branch coverage. The attended Studio E2E passed with Allure publication. Independent source review, schema-rejection mutation controls, source/archive and installed/relocated checks, and the frozen intended-index boundary/metadata checks passed. Human acceptance remains open.
