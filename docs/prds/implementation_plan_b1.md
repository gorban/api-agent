# Strict Markdown executable Layout

## Checklist

- [ ] Use `layout.md` for LangGraph orchestration, with a versioned, deliberately restricted Markdown grammar. Let the implementor develop the exact allowed headings/table columns for the implemented behavior and show concrete declarations/parser diagnostics at human review; evolve the schema as needed. Configuration and pack-manifest formats remain separate choices.
- [ ] Parse Markdown structure into a typed Layout definition rather than treating arbitrary prose as instructions or extracting executable behavior with loose text matching. Accept only declared structural forms: fixed headings, fixed-column tables, inline identifiers, and inline file links; reject unknown executable sections/columns, duplicate IDs, invalid field types, unresolved functions/bindings, and ambiguous declarations with file/line diagnostics.
  - [ ] Represent nodes, unconditional/conditional edges, input/output bindings, independent loop groups/attempt budgets, and ordered conditional prompt sources using the same strict grammar. Named conditions resolve to registered Python functions; table cells do not contain arbitrary Python expressions.
  - [ ] Define optional nonexecuting notes explicitly so explanatory prose cannot accidentally alter the graph. Generate diagrams from the parsed definition; Markdown itself is the executable readable description, not a separate copy of the flow.
- [ ] Store engine-local prompt links as genuine inline Markdown links, with destinations always relative to the Markdown declaration containing the link. Resolve runtime paths against that file's parent directory, never the process working directory, pack root by accident, or repository root by convention.
  - [ ] Keep prompt row order as application order. An unconditional entry uses an explicit `always` marker; a conditional entry names its condition function. Validate every local link in the selected declaration, including conditional entries, before building the graph; report broken/invalid paths rather than silently omitting prompts.
  - [ ] Require relative local destinations and readable existing prompt files. Use normal portable URI escaping for filenames with spaces/special characters and normalize paths through a defined decoder. Do not confuse heading anchors, external URLs, or directory links with executable prompt files.
- [ ] Keep `<resource_pack>` explicitly in pack-specific prompt link destinations and visible link text. These are runtime-only links; editor/GitHub navigation is supported only for ordinary local file links. api-agent can select multiple different packs, so its checked-in Layout must not substitute a particular resource pack or GitHub repository as a fixed link target.
  - [ ] Use Markdown-escaped angle brackets in link text/destinations so the parsed destination retains the literal `<resource_pack>` token. Resolve that token against the selected pack root at runtime, then validate the linked prompt file, including conditional entries. Report unresolved variables and missing files before building the graph.
  - [ ] Do not require pack-owned prompt fragments to work around these intentionally unresolved links. Keep the ordered prompt entries in the Layout and distinguish declaration-relative paths from runtime pack-relative paths in the loader.
- [ ] Add deterministic parser/link validation for the installed engine and demo pack, selected resource pack, differing working directories, renamed/missing prompt files, conditional order, literal placeholder preservation, and resource-pack switching. Run Layout validation as a targeted developer check without executing an LLM. Static local-link checks must distinguish runtime-variable destinations; runtime checks validate them for the selected pack. Only claim paths are valid after their files exist and validation passes; the initial planning skeleton does not create them yet.

## Execution timeouts and progress

- [ ] Extend the strict Markdown grammar and typed Layout definition with optional timeout declarations for nodes, nested groups and the whole graph. Define an explicit unit (seconds) and omission syntax; accept only finite positive limits and reject invalid or ambiguous declarations with file/line diagnostics. Review the exact columns and examples with the implementation.
- [ ] Enforce declared deadlines in the runtime using monotonic clocks and owned cancellation/cleanup. A child or retry cannot extend its enclosing deadline. Define child-attempt timer boundaries explicitly; entering another node or retry must not restart a group/graph deadline. No timeout declaration means no additional deadline, not an invented default or a sum of component limits.
- [ ] Reuse and extend [shared component progress](../../src/api_agent/providers/progress.py) and [runner events](../../src/api_agent/testing/contracts.py), preserving scope identity and parent relationships. Emit start, elapsed updates and completion for untimed as well as timed components. Bind concurrent scopes to their own test/execution; use fixed descriptive names without prompts, credentials, account names or URLs.
- [ ] When a component has its own timeout, show its name, elapsed time and actual limit. Otherwise walk its enclosing scopes and show the name, elapsed time and limit of the nearest scope with a timeout, including a declared whole-test timeout if that is the only one. Use that scope's original clock; do not label an ancestor's limit as the child's or reset its elapsed time when children change.
- [ ] If neither the component nor any enclosing scope/test has a timeout, still display the current component's name and elapsed time, or test elapsed time when no component is active. Omit the timeout field entirely. Keep the shared running-row/summary presentation for unit, integration and E2E; remove finished progress and reject stale events as today. See [shared test-display follow-through](implementation_plan_a.md#active-component-timeout-progress).
- [ ] Add deterministic parser, runtime and presentation tests for timed nodes, untimed children under timed groups/graphs/tests, nested timed-scope precedence, fully untimed execution, retries without parent-clock resets, parallel tests, deadline expiry and synchronous cleanup. Verify real runner event delivery with synthetic nodes; these checks need no live LLM calls. Record declared scope/timeout and timeout failures in integration/E2E Allure evidence; unit tests remain outside Allure.

## Illustrative readable declaration

The exact schema will be reviewed before implementation. This example is located conceptually at `api-agent/layout.md`; its ordinary relative path refers to the planned skeleton, not to this planning document. The pack prompt deliberately retains `<resource_pack>` and cannot be followed on GitHub.

```markdown
# LangGraph Layout v1

## Nodes

| ID | Function |
| --- | --- |
| renderer | render_response |

## Edges

| From | To | When |
| --- | --- | --- |
| START | renderer | always |
| renderer | END | always |

## Prompts

| Node | File | When |
| --- | --- | --- |
| renderer | [Response contract](../src/api_agent/workflows/rendering/renderer/prompts/response_contract.md) | always |
| renderer | [Say Ok from \<resource_pack\>](../\<resource_pack\>/workflows/rendering/renderer/prompts/say_ok.md) | always |
```

[GitHub's relative-link documentation](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#relative-links) confirms that ordinary relative Markdown links are relative to their containing file and follow the viewed branch. Editor link navigation depends on the editor's Markdown support. Neither resolves `<resource_pack>`; the runtime loader supplies that resolution and validates the selected pack's paths.
