# Complete tool evidence in controlled application reports

[Named-tool reporting](implementation_plan_o4h.md)

- [x] Declare workflow credential authority for controlled graph integration
  cases so the launch-owned content policy and scrub inventory reach the graph.
  Explicit content opt-out must still withhold request/result values. Never
  infer permission from a raw configuration mapping or bypass credential leases.
- [x] Record one typed tool lifecycle per execution. Reuse the verified single-use
  gate identity for the tool observation and final application cause; remove the
  demo's redundant generic call rather than inventing inference accounting.
  Preserve actual tool timing, partial/failure results and exception propagation.
- [x] Show bounded input/output capture states on the tool step even when values
  are withheld or unavailable. Under permitted capture, attach the exact scrubbed
  tool request and response to that invocation, including partial data and errors.
- [x] Verify content opt-in/out, exact identity correlation, no duplicate generic
  call, redaction, scoped attachment ownership, full applicable coverage and
  independent external review. Inspect a newly generated Allure report.
- [ ] Human acceptance sees the returned synthetic item and partial error in the
  tool response, or an explicit withheld state when content is disabled.

The optional canonical identifier on the internal `ToolObservation` helper
does not change tool-result or worker wire contracts. The single-use verified
gate already owns the identity; consumer grants retain their existing fields.

Verification: 27,417 applicable unit tests pass with exact 100% line and branch
coverage. All six controlled integration cases pass with Allure publication; a
separate explicit content-disabled case passes and retains withheld states with
no tool attachments. These cases make no inference calls. Four causal controls
detect duplicate calls, split identities, missing capture-state labels and
legacy duplicate trace spans.
Independent source review and browser inspection of the new report pass. Existing
immutable reports cannot gain payloads that were never captured; rerun the test
to obtain the corrected evidence.
