# Future workflow differences and test-impact discovery

[Inspector requirements](implementation_plan_b1c.md) · [Recorded navigation](implementation_plan_b1c2.md)

These are design/refinement tasks after the currently scheduled work. Resolve entry points, matching rules, privacy and evidence limits before implementation; no feature is claimed here.

## Compare two recorded runs

- [ ] Select two authorized finalized runs and their exact captured/verified workflow declarations. Distinguish definition changes from execution differences, settings, timings and costs. Missing historical definitions, ambiguous node matches and incomplete evidence must remain explicit; never substitute today's graph silently.
- [ ] Explore a comparison overlay in the workflow viewer, reached from that viewer or the Allure archive. Highlight deleted items red with a strike-through, changed items orange, and wholly added items green. An otherwise retained ancestor becomes changed when a descendant changes. Keep these colors separate from execution success/error colors, with labels/icons and an accessible textual diff.
- [ ] Support progressive discovery from graph to component, node, bindings and prompt/resource details. Define identity/rename matching, additions/deletions, retained ancestry and side-by-side source details before freezing a schema. Preserve capture policy and secret masking; differences do not authorize confidential content disclosure or execution.

## Discover tests that exercised an area

- [ ] From a selected component, node or observed path, list recent compatible tests whose retained evidence proves they exercised it. Disclose graph identity, history window, attempt/run, missingness and stale/renamed identities. Distinguish observed coverage from proof that a test detects every possible regression; do not infer visits from declarations alone.
- [ ] Group integration and E2E identities separately. Offer selectable/copyable space-separated positional selectors and complete `./launch.py --tui-mode integration ...` or `./launch.py --tui-mode e2e ...` commands. Use the real stable collected identities, deduplicate, shell-quote safely and never execute on copy. Preserve owner/configuration prerequisites and explain unavailable selections.
- [ ] Consider a changed-area-to-tests workflow from the diff overlay, with bounded indexed queries and progressive disclosure. Before delivery, freeze compatibility/matching, command portability, privacy and retention limits; independently verify missing evidence, duplicate/renamed tests, shell metacharacters, accessibility and actual collector selection.
