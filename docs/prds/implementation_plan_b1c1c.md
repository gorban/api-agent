# Provider-owned nested JSON capture admission

[Execution evidence](implementation_plan_b1c1.md)

Provider-owned SDK responses can contain a safe final answer beside deeper native
reasoning metadata. Original JSON admission must not discard the entire response
merely because a sibling field exceeds the published value depth. This capture
repair is diagnostic; it does not validate a provider's output schema or establish
the cause of any particular rejected response.

- [x] For provider-owned content only, admit original JSON through the existing
  privacy boundary's depth of 20 before credential projection. Keep ordinary
  node capture, selected values, and published journal validation at depth 8.
  Retain the existing item, byte, type, cycle and field-name limits.
- [x] Keep credential authority across nested native and final-answer carriers.
  Continue excluding opaque/encrypted reasoning and native reasoning signatures; deeper plaintext
  fields outside the selected depth remain omitted or explicitly truncated.
  Do not broaden schema matching or retry schema-invalid responses.
- [x] Verify the local diagnostic and journal path with a synthetic nested SDK
  response: permitted final text survives while native reasoning state does not.
  Test cross-carrier credentials, ordinary depth limits, provider depth and byte
  limits, and original schema-rejection evidence without real inference.
- [x] Independently review the change and renew exact source coverage, package
  and public-boundary checks before implementation acceptance.
- [ ] Human acceptance: inspect an authorized content-enabled run's sanitized
  response capture and determine the actual output-schema failure separately.
