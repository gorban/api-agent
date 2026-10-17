# Multi-run history and viewer prerequisites

[Allure viewers](implementation_plan_d1.md) · [Selected-test navigation](implementation_plan_d1a.md)

## History browsing

- [x] Main-menu Allure serve browses finalized integration/E2E runs for the selected repository/pack, newest first. Enumerate finalized manifests below configured local/S3 roots; passing a parent directory to Allure 2 does not recursively discover run folders. Do not apply the cost-history lookback or let units replace this history.
- [x] Preserve each run's identity through separate immutable report routes. Repeated tests across different suites remain separate runs; do not silently collapse history into Allure retries. Generate a run's report when selected rather than rebuilding every historical run at startup. Refresh discovers new manifests and preserves existing report bindings.
- [x] Select by persisted identity and timestamps, never filesystem modification times. Validate manifest schema, repository/run identity, every artifact hash and attachment reference before generation. Reject changed/conflicting manifests, missing/corrupt files, links and nonregular files with actionable errors. Preserve original bytes and immutable history.
- [x] Support explicitly empty ALLURE_RESULTS_DIR by reading exact finalized S3 artifacts into owned temporary staging. Storage retrieval never starts LocalStack or an authentication flow merely to browse. SDK transport timeouts and shared operation budgets bound retrieval; cancellation drains owned work.
- [x] Serve only the owned loopback socket, with Host validation and no directory listing. ALLURE_VIEWER_PORT defaults to 5252. An occupied port fails clearly without adoption, replacement or silent port changes. Ordinary generation is a snapshot; uploads do not silently replace a served report.

## CLI and browser

- [x] Require an externally installed Allure 2 CLI available as `allure` on PATH in the launcher's environment. Check availability/version under bounded progress; report missing CLI, incompatible versions, Java startup failures and generation timeouts. Never silently install software. Recording/publication remains usable without this optional viewer dependency.
- [x] Document `brew install allure` for macOS and Homebrew Linux; the formula supplies OpenJDK. Link other platforms to official installation guidance. A host installation is not assumed visible inside containers. See [development prerequisites](../development.md).
- [x] Open a managed headed browser after server readiness; display its URL and inspection endpoint. If browser opening fails, retain the report and show its URL without altering test outcomes. Reuse browser/process ownership and retryable cleanup. Quit/leave-viewer stops only owned viewer resources, preserving shared storage and other sessions.

## Acceptance

- [x] Independently review controlled CLI/HTTP/browser, local/S3 integrity, malformed/late history, conflict, cancellation, process and retry controls. Require exact applicable coverage, installed-package independence, licenses and public-boundary checks.
- [ ] Human review multiple run routes, refresh, browser-opening failure diagnostics and owned cleanup. Selected-test/live-preview acceptance remains in its own child.

Official references: [generation](https://allurereport.org/docs/v2/generate-report/), [installation](https://allurereport.org/docs/v2/install/) and [Homebrew formula](https://formulae.brew.sh/formula/allure).
