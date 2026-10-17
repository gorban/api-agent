# Incremental native-history preparation

[Native history](implementation_plan_d1c.md) · [Results lifetime](implementation_plan_d1e.md)

- [ ] Profile first-open, unchanged reopen and refresh after additional finalized runs. Record discovery, verification, download and Allure CLI generation separately with representative controlled histories.
- [ ] Reuse validated derived reports/history prefixes when beneficial, keyed by immutable artifact hashes, ordered history context, mode, CLI version and rendering policy. Reconcile manifests each refresh, including late uploads; rebuild only affected suffixes. Preserve the bounded recent-history window, archive access, exact attempt links and original stored bytes.
- [ ] Any persistent cache must have explicit byte/age limits, transactional publication, concurrent-owner protection and corruption recovery. Do not expose partially generated routes. Closing a viewer still disposes its browser/server; caching cannot keep inspection resources alive or initiate authentication.
- [ ] Verify unchanged reuse, appended and late runs, changed history windows, corruption, CLI changes, cancellation, concurrency, package/boundary checks and native Allure history equivalence. Report measured benefit; no optimization is required without material improvement.

This is deferred performance work, after correctness and current viewer acceptance. Existing immutable routes and publication remain authoritative.
