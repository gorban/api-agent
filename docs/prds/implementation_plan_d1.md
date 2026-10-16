# Selected-run report viewers

[Allure reporting](implementation_plan_d.md) · [Shared reporting management](implementation_plan_h2a2.md).

## Serving and navigation

- [ ] Provide the TUI's "Allure serve" mode for viewing test results. Default to the most recent recorded integration/E2E run for the selected repository/pack; unit runs must not replace that selection.
  - [ ] When ALLURE_RESULTS_DIR is explicitly empty, fetch the selected S3 run into owned temporary viewer staging and clean it after serving; do not assume a retained local directory exists.
  - [ ] After the report server is ready, automatically open its URL in the host's default browser, including when entering Allure mode through the `a` shortcut. Support host-browser forwarding when the TUI runs inside Distrobox. Also display the URL; if browser opening fails, keep the report available and report the opening failure.
  - [ ] Track completed/finalized run identities in persistent history and resolve the latest run from run metadata, not arbitrary file modification times or stale cached reports. Load the selected run's artifacts locally or from S3 as needed without combining results from unrelated runs.
  - [ ] Support `a` from the completed test-list/summary screen as an immediate transition into Allure mode, pinned to that screen's run ID. A concurrently completed run must not redirect this shortcut away from the results the user was viewing. If no recorded run exists, show that clearly.
  - [ ] Test latest-run selection and the results-to-Allure transition deterministically, including failed/interrupted finalized runs, newer concurrent runs, and a subsequent unit run.

## Report-viewer sharing

- [ ] Bind a generated Allure viewer to the exact selected immutable run and
  manifest. Ordinary report generation/serving does not automatically incorporate
  newly uploaded S3 results. Reuse a managed viewer only for the same verified
  binding, or implement an explicit multi-run viewer with separate run routes.
  A different run or foreign listener on the configured port must fail clearly,
  without replacing the other viewer, changing ports or substituting latest data.
- [ ] Keep result publication independent of viewer lifetime. Sharing LocalStack
  or a tracing backend does not imply that their UI/run/session state can be
  adopted. Verify two view requests for the same and different runs, finalized
  late uploads, stale viewer identity and owned stop before human acceptance.

[Allure 2 generation/serve documentation](https://allurereport.org/docs/v2/generate-report/)
describes a generated report and a server for that report directory; dynamic
history browsing must be implemented explicitly rather than assumed from S3.
