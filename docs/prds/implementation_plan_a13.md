# Viewer modes in the shared launcher menu

[Python TUI](implementation_plan_a.md) · [Flow viewer](../workflow_viewer.md) · [Trace viewer](implementation_plan_k1b3.md)

This is a future launcher/TUI change. Existing top-level viewer flags remain
available until this migration is implemented and qualified.

## Dispatch and main menu

- [ ] Make View flow and Trace viewer ordinary TUI modes, selected through `--tui-mode view-flow` and `--tui-mode trace-viewer`. Remove the top-level `--view-flow` and `--trace-viewer` options as part of this migration. Update help and give actionable replacement syntax for obsolete options; do not retain competing dispatch paths.
- [ ] Present this two-column order, retaining existing behavior for all other actions:

| Left | Right |
| --- | --- |
| Run local | Unit tests w/ coverage |
| Integration tests | E2E tests |
| Allure serve | View flow |
| Trace viewer | Deployment logs |
| Clear .env configuration | Container management |
| Setup unattended authentication | Review configuration |

- [ ] Preserve conditional menu availability. Container management remains disabled under its existing eligibility rules, including when no running containers are available. Arrow, standard Vim `hjkl`, focus and activation behavior must skip disabled/unavailable entries in both directions and across rows/columns. Revalidation must not leave focus on a newly disabled entry or dismiss an open confirmation dialog.
- [ ] Preserve conditional Review configuration availability for applicable, non-hidden settings missing from `.env` under the existing acknowledgement rules. Adding viewer modes must not make Review permanently visible, expose unrelated hidden settings or weaken normal configuration precedence.
- [ ] Add the main-menu shortcut `* Star api-agent on GitHub`, visibly advertised alongside menu shortcuts without changing the six-row operation grid above. Scope it to the main menu; preserve existing `*` meanings on log and test screens. Invoking it opens a headed managed Playwright browser directly at `https://github.com/gorban/api-agent` and automates starring that repository.
- [ ] Make starring idempotent: verify the exact repository and authenticated GitHub session, click Star only when it is not already starred, and confirm the resulting state. Never toggle Unstar, change accounts or apply the action to another repository. If sign-in is needed, leave the browser for the user's normal sign-in and continue after authentication; do not silently acquire credentials, create an account or bypass challenges. Show useful waiting/success/already-starred/failure status and leave normal menu navigation usable. No GitHub login/network activity occurs merely from opening the menu.
- [ ] Reuse managed browser/inspection ownership, bounded action progress and cancellation-safe cleanup. Closing/cancelling the browser before completion leaves the menu usable and reports incomplete starring rather than claiming success; Quit disposes owned resources. Keep account tokens, cookies and credentials out of logs and artifacts.

## View flow

- [ ] Provide an interactive picker for eligible workflow documents, reusing the selected application's and declared pack/test workflow inventory and existing path/resource admission. Allow exactly one selected workflow; do not reuse multi-test toggles or range selection. Default selection is the selected application's root flow; Enter opens the selected document through the managed viewer.
- [ ] Ctrl+C copies the selected document's full absolute path without quitting or launching it. Preserve scrolling, keyboard/Vim navigation, full-path visibility/copy and responsive selection for large inventories.
- [ ] With `--no-std-in`, preserve current behavior: default to the selected application's root flow or accept one optional positional path. Retain current relative-path resolution, quoted paths with spaces, validation, temporary preparation for unstaged changes, no-Git operation and admitted resource links. Reject extra positional paths actionably.
- [ ] Reuse the existing managed viewer/browser owner rather than introducing a second renderer or server. Specify launch/back/stop behavior consistently with existing operation ownership, and retain cleanup on failure, interruption and menu/quit transitions.

## Trace viewer and configuration repair

- [ ] Enter the interactive viewer operation only when the same effective configuration that currently admits standalone `--trace-viewer` is valid. Disabled tracing, external/custom targets and missing/invalid relevant settings must not start a collector or browser. Reuse provider capability admission when [pack tracing providers](implementation_plan_k1a2.md) are delivered.
- [ ] Resolve one actionable blocker at a time in a stable order. If its offending value is saved in `.env`, open Clear .env configuration; otherwise open Review configuration. Reuse the existing operation-required header and red footer-adjacent diagnostic, with the field and viewer-mode context. Distinguish a missing required value from a present but unsupported destination. Preserve applicability and saved-value provenance rather than guessing from error prose.
- [ ] Carry the trace-viewer continuation intent only through its own repair screen. On `c Close`, re-resolve configuration: continue to the viewer if it is now valid; otherwise return to the main menu. Do not trap the user in repeated redirects, start a blocked viewer or change the return behavior of independently opened Clear/Review screens. Refresh the displayed blocker as corrections expose another requirement, without listing all failures at once.
- [ ] Preserve headed browser/CDP presentation, optional dashboard login, explicitly permitted intended-login display and existing shared collector ownership. Closing Playwright must leave the command/operation and collector usable from another browser until explicit Stop, Ctrl+C or owner disposal. Menu return and quit must clean up safely; never remove retained trace volumes or disrupt another consumer.
- [ ] With `--no-std-in`, preserve current standalone trace-viewer behavior through the new mode: useful admission errors/nonzero exit without repair screens, lazy startup, console viewer/CDP/login output, and Ctrl+C cleanup. Do not force unrelated application/provider authentication or configuration acknowledgement.

## Migration and acceptance

- [ ] Update README, launcher/help, workbench/tracing/workflow-viewer guides, examples, tests and generated workflow-document viewing instructions to the new mode syntax. Keep optional paths double-quoted in generated instructions and omit the path for the root flow. Regenerate admitted resources through the shared provenance checks; preserve historical evidence instead of rewriting its original commands.
- [ ] Verify exact menu layout, conditional visibility/disabled-state refresh, bidirectional navigation, selection/path copy, startup mode dispatch and noninteractive parity. Exercise each configuration-blocker source, multiple successive blockers, Close-to-resume/Close-to-menu and ordinary independent Clear/Review returns. Prove invalid admission performs no allocation.
- [ ] Verify the GitHub shortcut with controlled pages for signed-out sign-in continuation, unstarred/already-starred state, wrong repository, challenge/cancellation, action failure and menu/quit cleanup. Assert at most one Star action and no Unstar action, and preserve `*` on other screens. Real-account starring is an opt-in human check of this shortcut, not an ordinary unit/integration side effect; merely selecting or loading the menu never grants that action.
- [ ] Exercise browser closure, explicit stop, cancellation, failed startup/cleanup, shared leases and retained trace data. Cover installed/relocated/no-Git launches, documentation links, meaningful causal controls, exact applicable coverage, boundary/license/provenance checks and independent/external review before separate human acceptance.
- [ ] Obtain human acceptance of both menu entries, flow selection/copy and trace configuration repair, including noninteractive equivalents.
