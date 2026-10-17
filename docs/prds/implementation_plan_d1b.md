# Multi-run history and viewer prerequisites

[Allure viewers](implementation_plan_d1.md) · [Selected-test navigation](implementation_plan_d1a.md)

## History browsing

- [x] Main-menu Allure serve discovers finalized integration/E2E runs for the selected repository/pack, newest first. Enumerate finalized manifests below configured local/S3 roots; passing a parent directory to Allure 2 does not recursively discover run folders. Do not apply the cost-history lookback or let units replace this history.
- [x] Preserve each run's identity through separate immutable report routes. Repeated tests across different suites remain separate runs; do not silently collapse history into Allure retries. Generate older archive reports when selected. The [native-history follow-through](implementation_plan_d1c.md) builds bounded native chains when an archive row is opened; the complete archive is the main entry point. Refresh discovers new manifests and preserves existing report bindings.
- [x] Select by persisted identity and timestamps, never filesystem modification times. Validate manifest schema, repository/run identity, every artifact hash and attachment reference before generation. Reject changed/conflicting manifests, missing/corrupt files, links and nonregular files with actionable errors. Preserve original bytes and immutable history.
  Preserve manifest-listed `environment.properties` exactly; viewer annotations are added only when that filename is absent.
- [x] Support explicitly empty ALLURE_RESULTS_DIR by reading exact finalized S3 artifacts into owned temporary staging. Storage retrieval never starts LocalStack or an authentication flow merely to browse. SDK transport timeouts and shared operation budgets bound retrieval; cancellation drains owned work.
- [x] Serve only the owned loopback socket, with Host validation and no directory listing. ALLURE_VIEWER_PORT defaults to 5252. An occupied port fails clearly without adoption, replacement or silent port changes. Ordinary generation is a snapshot; uploads do not silently replace a served report.

## Secondary history source

- [x] Add visible boolean ALLURE_VIEW_SECONDARY, default False, with normal configuration precedence. True renders finalized history exclusively from ALLURE_SECONDARY_DIR, with the same integrity checks and no S3 retrieval, authentication or LocalStack startup. False preserves working-results/S3 history discovery. Exact selected-test previews remain bound to their current attempt.
- [x] Require a nonempty ALLURE_SECONDARY_DIR when the toggle is True in integration, E2E and Allure viewing modes. Collection-only and unrelated modes remain unaffected. Keep secondary-directory configuration visible in Allure mode too.
- [x] Keep publication independent: integration/E2E still require primary S3 and always attempt an additional secondary copy whenever its directory is configured, with either viewing policy. Never mutate or republish history merely to view it.
- [x] Refresh archive bindings from the currently selected source even when the manifest and frozen history context already have a route. Switching to secondary-only viewing must not read the earlier primary source through a refreshed archive link. Preserve immutable report routes, bytes and history contexts.
- [x] Verify configuration precedence/visibility, source isolation and missing/empty paths, publication independence, installed-package behavior and exact applicable coverage.

- [x] Independently recheck secondary-source selection and refreshed archive links, preserving earlier viewer closures.
- [ ] Human review toggle visibility and secondary-only browsing with a configured existing directory. No new inference is needed.

## CLI and browser

- [x] Require an externally installed Allure 2 CLI available as `allure` on PATH in the launcher's environment. Check availability/version under bounded progress; report missing CLI, incompatible versions, Java startup failures and generation timeouts. Never silently install software. Recording/publication remains usable without this optional viewer dependency.
- [x] Document `brew install allure` for macOS and Homebrew Linux; the formula supplies OpenJDK. Link other platforms to official installation guidance. A host installation is not assumed visible inside containers. See [development prerequisites](../development.md).
- [x] Open a managed headed browser after server readiness; display its URL and inspection endpoint. If browser opening fails, retain the report and show its URL without altering test outcomes. Reuse browser/process ownership and retryable cleanup. Quit/leave-viewer stops only owned viewer resources, preserving shared storage and other sessions.

## Acceptance

- [x] Independently review controlled CLI/HTTP/browser, local/S3 integrity, malformed/late history, conflict, cancellation, process and retry controls. Require exact applicable coverage, installed-package independence, licenses and public-boundary checks.
- [ ] Human review multiple run routes, refresh, browser-opening failure diagnostics and owned cleanup. Selected-test/live-preview acceptance remains in its own child.

Native cross-launch history: [requirements and acceptance](implementation_plan_d1c.md).

Official references: [generation](https://allurereport.org/docs/v2/generate-report/), [installation](https://allurereport.org/docs/v2/install/) and [Homebrew formula](https://formulae.brew.sh/formula/allure).

- [x] Add review-visible ALLURE_CLI, default allure, accepting a quoted executable and optional command-prefix arguments. Parse to an argument vector without shell execution. Use the same prefix for bounded version checking and generation; missing commands, malformed quoting and nonzero exits are actionable. Document host/container PATH differences and explicit host executable paths.
