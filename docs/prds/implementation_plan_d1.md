# Selected-run report viewers

[Allure reporting](implementation_plan_d.md) · [Shared reporting management](implementation_plan_h2a2.md).

## Serving and navigation

- [ ] Provide the TUI's "Allure serve" mode for viewing test results. Default to the most recent recorded integration/E2E run for the selected repository/pack; unit runs must not replace that selection.
  - [ ] When ALLURE_RESULTS_DIR is explicitly empty, fetch the selected S3 run into owned temporary viewer staging and clean it after serving; do not assume a retained local directory exists.
  - [ ] After the report server is ready, open a headed browser through the shared managed browser lifecycle, including the test shortcut. Display its URL and inspection endpoint where appropriate; support the launcher's container/host access path. If browser opening fails, keep the owned report available and report the opening failure without changing test outcomes.
  - [ ] Resolve the latest finalized run from persistent run metadata. Fetch exact local/S3 artifacts without mixing runs or using file modification times/stale reports.
  - [ ] Deliver [selected-test navigation during and after execution](implementation_plan_d1a.md): `a View in Allure` opens the one completed test/attempt at its exact run identity, without navigating away from the running results screen. Hide it for multi-row selections and group rows.
  - [ ] Test latest-run and exact-test navigation, partial-suite viewing, failed/interrupted runs, newer history, unit runs and managed cleanup. The selected-test slice precedes broader reporting management; it does not start tracing or alter shared storage ownership.

## Report-viewer sharing

- [ ] Bind a finalized Allure viewer to the exact selected immutable run and
  manifest. The [live-results preview](implementation_plan_d1a.md) instead uses
  an explicit operation-owned snapshot binding until final publication. Ordinary report generation/serving does not automatically incorporate
  newly uploaded S3 results. Reuse a managed viewer only for the same verified
  binding, or implement an explicit multi-run viewer with separate run routes.
  A different run or foreign listener on the configured port must fail clearly,
  without replacing the other viewer, changing ports or substituting latest data.
- [ ] Keep result publication independent of viewer lifetime. Sharing LocalStack
  or a tracing backend does not imply that their UI/run/session state can be
  adopted. Verify two view requests for the same and different runs, finalized
  late uploads, stale viewer identity and owned stop before human acceptance.

[Allure 2 generation/serve documentation](https://allurereport.org/docs/v2/generate-report/)
describes generated reports; S3 updates require explicit regeneration.
