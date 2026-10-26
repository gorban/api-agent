# Allure history and selected-test viewers

[Allure reporting](implementation_plan_d.md) · [Shared reporting management](implementation_plan_h2a2.md)

## Viewer capabilities

- [x] Complete [multi-run history, CLI prerequisites and managed serving](implementation_plan_d1b.md): separate immutable finalized integration/E2E reports for the selected repository/pack, newest first. Units stay outside history. [Native cross-launch history](implementation_plan_d1c.md) gates renewed acceptance.
- [x] Complete [exact-test navigation during and after execution](implementation_plan_d1a.md). `a View in Allure` opens the highlighted terminal test/attempt, including while other tests run. Exclude groups, pending/running tests and units; [selected-range viewing](implementation_plan_d1i.md) extends the original single-row contract.
- [x] Keep final history and operation-owned live previews distinct. Each preview has an explicit version and artifact hashes; later tests/final publication require a new binding. Never publish a partial finalized manifest or silently substitute latest data. Completed accounting and immutable final manifests remain authoritative.
- [x] Deliver [whole wrapped-link interaction and CDP clarity](implementation_plan_d1d.md) for viewer locations.

- [x] Complete [live availability and results-lifetime refinement](implementation_plan_d1e.md).


- [x] Deliver [selected ranges and whole-suite viewing](implementation_plan_d1i.md), preserving exact attempt identity and immutable preview/history guarantees.

## Ownership and sharing

- [x] Bind each finalized route to its exact run/manifest and each preview to its frozen snapshot. Reuse only the same verified binding. Ordinary generation does not automatically incorporate S3 uploads; refresh adds newly verified history without replacing existing reports.
- [x] Keep publication independent of viewer lifetime. Sharing LocalStack or tracing does not authorize adopting unrelated UI/run/session state. A foreign listener fails clearly; never replace it or change ports silently. Closing a viewer must not cancel tests or stop shared reporting storage.
- [x] Finish independent joined review, applicable exact coverage and installed/package/license/boundary checks. This bounded viewer does not implement tracing management or last-session container shutdown.

The optional CLI has [standalone installation instructions](../development.md). Application E2E retains its separate execution gate and reuses these viewer contracts.

## Platform hardening

- [ ] Verify and harden [native Windows listener isolation](implementation_plan_d1g.md) with explicit socket policy and native acceptance. Preserving its current non-reuse setting does not establish exclusive binding.

## Later optimization

- [ ] Profile and conditionally optimize [incremental native-history preparation](implementation_plan_d1f.md) late in implementation.

**Deferred human review**

Functional viewer implementation and independent verification are complete. Explicit human acceptance remains unchecked in the linked children. Windows hardening and profiling remain separate unfinished tasks above.

- [History and managed serving](implementation_plan_d1b.md)
- [Selected-test navigation](implementation_plan_d1a.md)
- [Native cross-launch history](implementation_plan_d1c.md)
- [Wrapped-link interaction and CDP clarity](implementation_plan_d1d.md)
- [Results availability and lifetime](implementation_plan_d1e.md)

- [Selected ranges and whole-suite reports](implementation_plan_d1i.md)
