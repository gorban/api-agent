# Workflow evidence projection

[Reporting requirements](implementation_plan_d.md).

- [x] Validate incoming workflow evidence through the existing journal and legacy scope renderers. Project one graph view when both packets describe the same admitted execution, while retaining original attachments and authoritative accounting unchanged.
- [x] Match admitted scope and invocation identities rather than display names. Preserve distinct graph executions, attempts, retries, visits, additional call/child evidence and legacy-only packets. Never join an ambiguous legacy scope to an arbitrary invocation.
- [x] Preserve journal call enrichment when joining a compatible legacy view. Reject conflicting same-identity parentage or attempt/timeout metadata visibly instead of silently replacing evidence.
- [x] Verify the actual workflow driver's dual-publication path, multiple executions with retries, both packet orders and repeated packets. Independently replay a recorded normal run and use causal checks for missing deduplication and conflict validation.
- [x] Complete applicable exact unit coverage, installed-artifact and public-boundary checks, and verify the ordinary recorded integration report after the correction.
- [x] Complete external review of the corrected report. Existing execution qualification and prior human acceptance retain their original scope.
- [ ] Obtain explicit human review of the corrected report.

## Ambiguous packet order

- [x] Preserve legacy provenance until invocation attribution is unambiguous. If multiple journal invocations claim the same scope, retain the legacy-only fields independently for every packet order, including legacy first, without moving them to an arbitrary invocation.

**Deferred human review**

Technical implementation, independent and external reviews are complete. Only explicit human acceptance of the corrected report remains for this bounded projection.
