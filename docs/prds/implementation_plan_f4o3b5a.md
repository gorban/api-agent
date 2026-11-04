# Native metadata evidence and detail identities

[Native reasoning requirements](implementation_plan_f4o3b5.md)

## Requirements

- [x] Project native inference evidence through complete recognized schemas.
  Unrecognized response and error metadata wrappers recursively withhold known
  native carrier fields and opaque native objects. The projection is bounded by
  depth, item count and text work, and cannot modify native replay state.
- [x] Sanitize provider error diagnostics before release, including serialized
  native metadata embedded in ordinary diagnostic prefixes. Encoded field names
  cannot bypass the bounded check. Ordinary permission, model and parameter
  diagnostics remain actionable, with status and known usage retained.
- [x] Preserve actual assistant plaintext, including JSON-shaped text mentioning
  native fields. Admitted user/tool text is separate from error metadata and is
  never interpreted as a diagnostic or rewritten to remove native-looking text.
- [x] Require unique non-null detail IDs and require every supplied detail index
  to equal its original array position. Omitted indices and nullable IDs remain
  supported. Accepted replay preserves the exact original order and values.

- [x] Give only the transport's actual outgoing capture call explicit request
  authority. Preserve validated effective generation controls and authored tool/
  structured schemas exactly, including fields named reasoning or signature.
  Continue projecting assistant continuation carriers out of captured evidence.
  Incoming response/error wrappers cannot gain request authority by their shape.

## Verification and acceptance

Synthetic normal transport tests exercise HTTP 200 and 400 on both API formats,
including nested opaque fields, serialized/prefixed error messages, unchanged
assistant JSON plaintext, exact replay and retained 5/7 token usage. Distinct IDs
isolate index checks; valid sequential indices isolate duplicate-ID checks.

The common detail fields follow the public
[OpenRouter reasoning contract](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#common-fields),
reviewed November 3, 2026. Detail identity validation does not establish that a
route passed live tool or puzzle qualification.

- [ ] Independent adversarial review of this metadata repair.
- [ ] Merged complete required checks and public-boundary review.
- [ ] Human acceptance remains with the parent native requirements.
