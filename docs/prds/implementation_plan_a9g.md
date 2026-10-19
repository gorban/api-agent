# Multi-test DEBUG confirmation

[Configuration navigation](implementation_plan_a9d.md)

## Requirements and acceptance

- [x] For integration and E2E execution with effective debug (`DEBUG=True`, or E2E-only `E2E_DEBUG=True`) and more than one distinct selected test, warn that up to the selected test count of browser windows could open. Tell operators to disable DEBUG and any E2E_DEBUG compatibility flag and omit `--debug` to avoid this. RUN_UI alone never requests inspection or this consent.
- [x] Require literal `y` to continue. In the TUI, use the existing confirmation modal. With `NO_STD_IN=True`, prompt on command-line stdin even for noninteractive runs and treat EOF, unavailable input, or any other answer as cancellation before authentication, resources, or the test runner.
- [x] Count selected identities after selector resolution, so overlapping groups count once. Leave unit, collection-only, and single-test execution ungated. The current E2E selector applies this consent before its existing acceptance screen; a complete E2E executor remains future work and must share effective debug across retention consumers.
- [x] If an integration run is cancelled while its DEBUG modal is open, cancellation wins over a simultaneous or later `y`; remove only that modal, settle the run lifecycle, and do not start authentication or the runner.
- [x] Human acceptance of interactive multi-test DEBUG confirmation is recorded.
- [ ] Human acceptance of the NO_STD_IN terminal path remains open pending the [terminal progress and inspection shutdown repair](implementation_plan_a9g1.md).

Focused tests cover the distinct-ID threshold, modes, warning text, and exact terminal response. An isolated threshold mutation makes the warning tests fail.

## Terminal completion and inspection follow-up

- [*] [Prompt parallel completion reporting and quiet owned inspection shutdown](implementation_plan_a9g1.md). This repair does not reopen accepted interactive confirmation.
