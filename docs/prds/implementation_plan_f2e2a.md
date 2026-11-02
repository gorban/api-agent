# Structured test-profile diagnostics

[Shared response scrubbing](implementation_plan_f2e2.md)

- [x] Keep profile diagnostic JSON structured through worker capture. Apply its expected schema and the final shared credential authority using the existing schema-aware scrubber before rendering successful-test stdout; do not flatten first and then apply a whole-record text mask.
- [x] Preserve safe fields and native types, empty only credential-bearing string values, and omit credential-bearing names through the shared mechanism. Validate required fields and minimum lengths. Diagnostics remain value-free and separate from the payload namespace.
- [x] Treat malformed, oversized or unusable diagnostic records as explicitly withheld local reporting content without changing functional test outcomes. Bound record count and size, deduplicate repeated profile properties, and preserve required failure reporting.
- [x] Recheck at finalization against newly registered credentials. Retain final serialized-output credential checks; no trusted-content flag, raw schema supplied by workers, or general plaintext-scrubbing bypass.
- [x] Preserve `warning` versus `info`, ordinary stdout/stderr protection, and the local-only detail boundary. This is a typed diagnostic path, not heuristic parsing of arbitrary printed JSON or a change to inference/authentication contracts.
- [x] Prove optional-field redaction with safe siblings retained, credential-bearing names, invalid required fields, encoded/cross-carrier credentials, worker-to-parent capture, repeated finalization and late registration. Complete applicable coverage, boundary and external review gates.
- [x] Human acceptance of readable safe profile JSON and redaction limited to affected content where the schema permits it.

Verification: 23,597 unit tests passed with exact 100% line and branch coverage. Focused serial/parallel capture, late-registration checks and three restored causal mutations passed. Source archive, relocated installation, workflow provenance, public-boundary and independent external review checks passed. A fresh Studio selection integration run and Allure publication passed; its retained local stdout contained readable typed profile JSON and selection evidence. Human acceptance confirmed for readable profile JSON.
