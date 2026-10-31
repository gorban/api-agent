# Full unit-run admission and temporary storage

[Unit-suite performance](implementation_plan_a9i.md)

- [x] After fresh collection, require literal `y` before an interactive unit run whose actual selection covers the full collected suite, whether selected by group or individually. Explain that full-suite execution in the TUI is slower, recommend `./launch.sh --tui-mode unit --no-std-in` for the full suite, and explain that the TUI is best for focused partial selections. Partial, integration and noninteractive runs remain unaffected.
- [ ] Investigate the reported concurrent coverage database failure using actual failure evidence and temporary-directory ownership. Preserve coverage, 60-second guards, selected identities and no automatic retries; do not delete unrelated temporary state or assume disk exhaustion from a SQLite error.
- [x] Isolate inherited managed-tool activation for unit tests while preserving explicit test opt-in and normal launcher preparation. Verify the inherited setting in a real pytest subprocess using synthetic tools, and keep this distinct from the unresolved database failure.
- [x] Verify admission, refusal, cancellation and recollection changes with causal tests, applicable unit tests, exact coverage and independent review.
- [x] Default unit execution and unselected-owner coverage scratch to private, uniquely owned user-cache directories; honor an explicit `TMPDIR`, contain child pytest temporary files and coverage, and clean owned directories after process disposal. Reject unsafe or unavailable cache locations with actionable errors; never remove shared parents or unrelated files. Verify real subprocesses, failure, cancellation and concurrency.
- [ ] Resolve the reported database failure with evidence and complete human acceptance.

Unit fixtures clear the inherited managed-tool marker for each test; tests can explicitly enable managed preparation. A subprocess regression exercises inherited isolation and explicit opt-in, and the existing viewer flow passes with the marker inherited. This does not establish the cause of the reported database failure.

## Confirmation copy feedback

- [x] Route selected warning/confirmation text through the shared clipboard handler, including native success, terminal-protocol fallback and copy failure feedback. Render the resulting toast on the active modal above its background without dismissing the warning, confirming execution or opening another Quit prompt. Preserve normal no-selection/input behavior and literal-y admission.
- [x] Verify actual keyboard dispatch and rendered toast placement with modal notifications enabled, not only a mocked notification call. Cover successful native copy, terminal fallback and failure, exact copied text and selection retention on failure, modal lifetime and cancelled execution, then complete applicable regression, coverage and independent/external review gates before human retest.

The feedback repair passed 21,931 unit tests with exact 100% line and branch coverage, plus independent and external source review. Package and public-boundary evidence is retained separately. The full-suite warning and modal copy feedback received human acceptance on October 30, 2026. This does not close the separate visible-window audit or coverage-database investigation.
