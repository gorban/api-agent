# Native Allure launch history

[History and prerequisites](implementation_plan_d1b.md) · [Selected tests](implementation_plan_d1a.md)

## Behavior

- [x] Main-menu Allure serve opens the latest finalized report with native History and trend data. Preserve repository/mode/file/test suite labels. A launch remains distinct from a retry; never pool independent launches as retry inputs.
- [x] Build native Allure 2 history chronologically from at most 20 launches of the requested mode, using persisted finish timestamps and run identity as a stable tie breaker. Use Allure-generated history rather than inventing result UIDs. Recent history is a bounded presentation window, independent of the cost-history lookback; the archive retains access to every finalized run.
- [x] Older launches remain lazily accessible from a secondary archive and native historical links, with original attachments and exact generated test identities. Expose the archive URL from the launcher. Refresh discovers new/late launches without changing existing report routes; freeze each route's history context as well as its manifest.
- [x] Read only verified finalized artifacts; preserve original results, manifests and metadata. Derived native history and default executor metadata live only in operation-owned staging. Mark the recorder-owned attempt counter excluded from test identity; upgrade legacy counters only in derived staging only for a nonempty file-qualified string fullName, its matching derived name and exact repository/mode/file labels, the matching recorder history ID and its sole `{name, value}` parameter named attempt with a canonical positive ASCII-integer string. Explicit flags, extra parameters/keys and ambiguous values remain unchanged, retaining the displayed counter and original stored bytes. Duplicate JSON members at any depth are ambiguous and the entire result stays byte-exact. Foreign parameters remain untouched. Never overwrite manifest-owned environment/executor files. Reject a history staging-name collision. Missing legacy history identifiers remain unavailable rather than guessed.
- [x] Bound native history transfer by bytes/files and the existing shared operation deadline. Close/cancel drains generation and disposes owned resources; failures expose no partially published route. Preview and exact-attempt behavior remain independent of the main history chain.

## Acceptance

- [x] Verify current-launch totals, failure/success history and trends, exact prior-test/attachment links, retries, independent modes, first launch, late upload/refresh, immutable context routes, stale/corrupt input, transfer limits and interrupted generation. Detect omitted history transfer and pooled-launch mutations.
- [x] Prove actual Allure CLI/HTTP/Chromium history navigation with controlled results, installed-package loading, exact applicable line/branch coverage and boundary/license/index/metadata checks; obtain independent review. No paid inference is needed.
- [ ] Human review the latest report, History tab, trend navigation, archive and exact selected attempt.

## Future format changes

Any later execution-trace format transition may offer an explicitly authorized reset instead of migration. Verify a local backup before deleting selected history, coordinate active writers and invalidate derived caches. Never make data deletion an automatic startup behavior. Trace and declaration overlays retain their own requirements.

Official formats: [history](https://allurereport.org/docs/how-it-works-history-files/), [executor](https://allurereport.org/docs/how-it-works-executor-file/) and [launches/retries](https://allurereport.org/docs/v2/generate-report/).
