# Allure history and selected-test viewers

[Allure reporting](implementation_plan_d.md) · [Shared reporting management](implementation_plan_h2a2.md)

## Viewer capabilities

- [x] Complete [multi-run history, CLI prerequisites and managed serving](implementation_plan_d1b.md). Main-menu Allure serve browses multiple finalized integration/E2E suites for the selected repository/pack, with separate immutable run routes and newest-first navigation. Units remain outside this history. Finish [native cross-launch history](implementation_plan_d1c.md) before renewed viewer acceptance.
- [x] Complete [exact-test navigation during and after execution](implementation_plan_d1a.md). `a View in Allure` opens the highlighted terminal test/attempt, including while other tests run. Hide it for multi-row selections, groups, pending/running tests and units.
- [x] Keep final history and operation-owned live previews distinct. Each preview has an explicit version and artifact hashes; later tests/final publication require a new binding. Never publish a partial finalized manifest or silently substitute latest data. Completed accounting and immutable final manifests remain authoritative.
- [x] Deliver [whole wrapped-link interaction and CDP clarity](implementation_plan_d1d.md) for viewer locations.

- [ ] Complete [live availability and results-lifetime refinement](implementation_plan_d1e.md).
- [ ] Profile and conditionally optimize [incremental native-history preparation](implementation_plan_d1f.md) late in implementation.

## Ownership and sharing

- [x] Bind each finalized route to its exact run/manifest and each preview to its frozen snapshot. Reuse only the same verified binding. Ordinary generation does not automatically incorporate S3 uploads; refresh adds newly verified history without replacing existing reports.
- [x] Keep publication independent of viewer lifetime. Sharing LocalStack or tracing does not authorize adopting unrelated UI/run/session state. A foreign listener fails clearly; never replace it or change ports silently. Closing a viewer must not cancel tests or stop shared reporting storage.
- [x] Finish independent joined review, applicable exact coverage and installed/package/license/boundary checks. This bounded viewer does not implement tracing management or last-session container shutdown.

The CLI is an optional out-of-band dependency; [installation instructions](../development.md) stand alone. Application E2E execution keeps its separate implementation gate; its eventual results reuse these generic viewer contracts.

**Deferred human review**

Substantive implementation and independent verification are complete. Explicit human acceptance remains unchecked in the linked children.

- [History and managed serving](implementation_plan_d1b.md)
- [Selected-test navigation](implementation_plan_d1a.md)
- [Native cross-launch history](implementation_plan_d1c.md)
- [Wrapped-link interaction and CDP clarity](implementation_plan_d1d.md)
