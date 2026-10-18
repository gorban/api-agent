# Multi-test DEBUG confirmation

[Configuration navigation](implementation_plan_a9d.md)

## Requirements and acceptance

- [x] For integration and E2E execution with `DEBUG=True` and more than one distinct selected test, warn that up to the selected test count of browser windows could open. Tell operators to set `DEBUG=False` and omit `--debug` to avoid this.
- [x] Require literal `y` to continue. In the TUI, use the existing confirmation modal. With `NO_STD_IN=True`, prompt on command-line stdin even for noninteractive runs and treat EOF, unavailable input, or any other answer as cancellation before authentication, resources, or the test runner.
- [x] Count selected identities after selector resolution, so overlapping groups count once. Leave unit, collection-only, and single-test execution ungated. Apply the same selection rule to E2E when execution becomes available.
- [x] If an integration run is cancelled while its DEBUG modal is open, cancellation wins over a simultaneous or later `y`; remove only that modal, settle the run lifecycle, and do not start authentication or the runner.
- [ ] Human acceptance: review the warning in an attended multi-test DEBUG selection and the terminal prompt in a noninteractive multi-test DEBUG selection.

Focused tests cover the distinct-ID threshold, modes, warning text, and exact terminal response. An isolated threshold mutation makes the warning tests fail.
