# Nested runtime interoperability

[Runtime and inference contracts](implementation_plan_b2a.md)

These future requirements are separate from supported nested observer ownership
and self-contained per-invocation journal reporting.

- [ ] Define and independently verify checkpoint configuration isolation when a
  V3 native node invokes a V2 workflow. Preserve explicit failures until the
  inner runtime can execute without inheriting incompatible serializer state.
- [ ] Preserve selected native admission, ownership, cancellation, deadlines and
  actual outputs across supported nesting formats; do not silently substitute
  checkpoint policies or suppress serialization errors.
- [ ] Qualify any global join of independent nested canonical invocation packets
  with a combined legacy observer tree. Preserve distinct invocation identities
  and render each actual graph once, without weakening strict parent checks.

## Acceptance

- [ ] All four outer/inner V2/V3 format combinations execute their actual parsed
  graphs under the declared checkpoint policy, including synchronous offload.
- [ ] Canonical journals remain independently complete and combined observer
  graphs retain their real structural parents across ordinary timing scopes.
- [ ] Global canonical/legacy joins have no duplicate graph or call rendering,
  with exact accounting and payload joins preserved.
- [ ] Focused causal checks and independent review verify execution, failure
  cleanup and historical evidence compatibility before support is claimed.
