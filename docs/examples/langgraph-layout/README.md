# Composable LangGraph Markdown example

This is a **design example**, not an executable application or a currently supported Layout grammar. It proposes readable declarations for a small generic planning/query/rendering flow, reusable timeout composition, and retained component timing. It neither reproduces the full corporate graph nor changes its existing behavior or deadlines. No production configuration is changed by these files.

Start with [layout.md](layout.md), follow the common [timeout component](components/timeout.md), and inspect the [configuration source](cfg/config.toml), [function contracts](functions.md), [prompts](prompts/planning.md), and [reporting convention](reporting.md).

## Example structure

```text
langgraph-layout/
    README.md
    layout.md                  # nodes, independent groups, bindings, prompts
    components/
        timeout.md             # one reusable wrapper contract
    cfg/
        config.toml            # typed defaults and optional aggregate limits
    functions.md               # proposed Python registry contracts
    prompts/
        planning.md
        verification.md
        rendering.md
    reporting.md               # live progress / retained Allure convention
```

Ordinary links are relative to their containing declaration. The example has no missing implementation-file links: functions not yet implemented link to their proposed contracts, and the timeout component links to the existing shared Python implementation. When real nodes/tools exist, declarations should link to their actual Python implementation/contract as well as their registry ID. Documentation links alone never authorize execution.

## What this makes visible

- The same timeout component wraps different nodes, groups, and individual operations without copying their timing logic.
- Every instance has an explicit name, lifetime, and configuration-field binding. An aggregate query allowance is distinct from the allowance for one GraphQL call or one LLM call.
- Prompt inclusion order, condition functions, tool ownership, and input/output references are readable at the point of composition.
- Group verification retries have independent counters. A retry does not restart its enclosing deadline; exception recovery is a separate declared policy.
- A generated viewer can expose the resolved configuration, source links, scope ancestry, and relevant execution evidence.

The existing [shared progress interface](../../../src/api_agent/providers/progress.py) and [TUI timing formatter](../../../src/api_agent/tui/test_results.py) already provide named scopes, elapsed time, and timeout context. Binding the proposed Layout and producing Allure component steps/attachments are still implementation work.

## Proposed parsing rules

Treat fixed headings/tables as typed declarations. This example extends the [restricted Layout plan](../../prds/implementation_plan_b1.md) with component instances, configuration bindings, and operation-level hooks; its exact columns must be reviewed before implementing the compiler.

1. `-` means an omitted optional declaration, not a numeric zero. Timeout values, when present, must be finite and positive seconds.
2. Resolve a field by the linked configuration declaration and field ID through the selected effective configuration, including defaults and explicit overrides. Do not reread a TOML default while ignoring the effective value.
3. Resolve callable/condition/tool IDs through the selected function registry. No Python expressions, arbitrary prose, or guessed imports execute from table cells.
4. Expand component instances into scoped wrappers; keep group/node/operation ancestry and qualified IDs. Reuse a definition while retaining independent instance state and clocks.
5. Invoke operation wrappers around each actual provider/tool call. Listing a tool in Markdown alone does not instrument nested calls; direct and repair paths must use the same registered adapter.
6. Bind inputs to runner values or outputs already produced on the executed path. Preserve the distinction between user content, plans, and tool evidence; report missing required bindings.
7. Validate applicable links, fields, registry IDs, routes, group ownership, and bindings before execution. Notes are explicitly nonexecuting.

## Timeout values and overrides

[cfg/config.toml](cfg/config.toml) uses the existing configuration metadata shape, including `acknowledge_default = false`. That hides advanced fields from routine TUI configuration review while preserving explicit `.env`, environment, and CLI values. Report effective value/source even for hidden fields.

The included 60-second values are **illustrative defaults for this example**, not proposed replacements for corporate settings. Optional group/graph limits have no invented default. Real integrations should commit their best confirmed defaults and preserve existing derived allowances before separately testing changes.

Once these declarations are selected by an implemented loader, intended override forms include:

```text
GRAPHQL_CALL_TIMEOUT_SECONDS=75
--graphql-call-timeout-seconds=75
--graphql-group-timeout-seconds=900
```

These show the configuration forms, not commands that run this unimplemented example. An explicit override changes the effective binding, not the source default. An absent optional group/graph limit adds no deadline; it does not remove any tighter ancestor or existing owner policy.

## Preserving an existing derived batch policy

A real integration may already calculate a batch allowance from request count, repair limits, and per-operation settings. Retain that named deterministic resolver and its inputs during migration. Bind its resolved value as the query-group timeout input and record the resolver/configuration/state provenance. This is an alternative to the static field binding shown here, not permission to execute a formula embedded in a table.

Do not replace the existing allowance with a short example graph cap, reset a parent clock at each call, or infer a new aggregate limit by summing worst-case children. The existing calculation can remain broad while reporting makes its implications visible. Tune it later using separate baseline/candidate acceptance evidence.

## Acceptance examples for the eventual compiler

These are behaviors to verify when implementation exists, not tests claimed to have run now:

- Renaming/removing a linked prompt yields a file/line validation error.
- A hidden timing field accepts a positive ENV/CLI override and rejects zero, negative, or nonfinite values.
- A child declaring 60 seconds with only 30 seconds remaining on its parent receives a fixed 30-second entry allowance. After 5 seconds its live display still says 30 seconds, while only 25 seconds remain for work.
- Untimed work under a timed ancestor uses that ancestor's name/clock in the live display; a fully untimed scope still shows elapsed time.
- A group retry preserves the group's original clock and uses the remaining budget. Another group's retry counter is unaffected.
- Repeated or concurrent tool calls have distinct scope IDs/ordinals and cannot contaminate another test's progress or evidence.
- A failed/cancelled scope records its actual outcome; a stopped heartbeat is not interpreted as success.
- A long passing execution remains discoverable by its named component durations and budget provenance in Allure.
- Parent duration is inclusive; nested child durations are not added again to create a fictitious total.
- Unit mode retains live progress without writing Allure artifacts.
