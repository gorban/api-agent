# Markdown workflow proof of concept

The runtime loads nested declarations from a selected `flow.md.html`,
captures their resources, and compiles registered LangGraph execution. The
[declaration grammar](prds/implementation_plan_b1b2.md) defines the inert source
format and resource bounds. The root example is a single chat component with one LLM call;
the permanent synthetic coverage fixture remains separate under
`demo/contrived/`. Each of two components owns
its producer, format check, guard and configurable pass budget. Fresh UUIDs
determine the coverage cases supplied to the real model; the test validates its
structured responses and executes the declared routes and retries.
The selected bundled pack uses the separate [verified API demonstration](api_demo.md).

Requirements and acceptance gates are in
[the B2 hierarchy](prds/implementation_plan_b2.md).

Open root `flow.md.html` directly for its saved static declaration/diagram, or
use `./launch.sh --view-flow "flow.md.html"` for its checked managed view.
Omitting the path instead opens the selected application's flow. See
[diagram maintenance and inspection](workflow_viewer.md) for markup-based
freshness, explicit generation and pre-commit checks.

Native nodes share an invocation-local mutable service view. Assigning a service
key affects later nodes in that invocation, but does not mutate the caller's
constructor dictionary or another invocation. To return additional runtime data
to the caller, use an explicitly owned mutable result carrier, as the bundled
application does with `result_store`. Give concurrent operations separate result
carriers; nested service objects are borrowed, not deep-copied.

## Implied owner columns

A table inside a node or component declaration may leave out the column that
names its owner, because the enclosing document already does:

| Document | Table | Optional column |
| --- | --- | --- |
| Node (`# Node:` or inline `## Node:`) | `Functions` | `ID`, and `Registry` (defaults to the node ID) |
| Node | `Bindings`, `Prompts` | `Node` |
| Node | `Timeouts` | `Owner` |
| Component | `Components` | `ID` |
| Component | `Timeouts` | `Owner` |

```markdown
### Functions

| Input | Output |
| --- | --- |
| [input](schemas/plan_input.json) | [output](schemas/plan_output.json) |
```

The loader inserts the omitted cells before any validation, so the shortened and
explicit forms yield the same rows, declaration digest and compiled-plan tables.
Only the declaration file's own bytes (and therefore `definition_id`) differ.
A column that is present must name the enclosing node or component. Root tables
and a component's `Bindings`/`Prompts` rows, which name descendant nodes, still
require their owner column. `Registry` is optional only when the column is
omitted; `-` is not a default marker.

## Ordered LLM retry histories

Both parsed-flow executors retain ordered history for `LLMRequest` calls through
`context.services['llm']` or `context.services['complete']`. These services accept
`LLMRequest` and return `LLMResult`; keep borrowed authority/context objects stable
within a dialogue, because distinct service arguments isolate their histories.
Each node and logical
dialogue keeps its earlier requests and assistant responses; later component
passes append their changed inputs and rejection feedback. Already accumulated
tool conversations keep their original calls, real results and opaque provider
continuations without duplication. The useful demo returns the reviewer's exact
`details` string in feedback, alongside the failed checks and prior candidate.

Use `inference_dialogue` from `api_agent.workflows.inference_history` when a node
has multiple independent roles, such as a conversation and separate tool
verifiers. Nested scopes can distinguish individual named tools. The included
[tool conversation](../src/api_agent/workflows/tool_conversation.py) illustrates
these scopes; the [useful demo](../demo/simple/functions.py) separates stable
conversation/tool definitions from changing candidate and feedback messages.

Histories are private, in-memory and local to one graph invocation, including
when content reporting is disabled. They do not carry across independent user
turns or provider owners. The caller's supplied conversation remains the starting
context. Provider/network clients outside the runtime service interfaces own
their own histories. Services remain borrowed and use their existing cleanup.

Retention is bounded to 128 dialogues and 32 MiB of serialized configuration and
message history per invocation. Existing 64-message and 2 MiB request bounds
still apply; further calls fail explicitly rather than trimming context. History
does not add inference attempts, relax guards, change cache policy or promise
cache hits. Provider serialization and credential controls remain authoritative;
see [history requirements](prds/implementation_plan_f1a1c.md) for acceptance gates.

Existing guarded components also support [deterministic candidate repair](workflow_repair.md)
within their declared pass/deadline budgets, preserving producer history and
blocking replay of dispatched effects.

## Declared prompts and resource-pack slots

Typed (State-declared) nodes declare their prompts beside their native binding,
so the template text is part of the admitted declaration. `{{slot}}`
placeholders name root `Prompt Slots` callables, which typically read a
resource the selected pack supplies through a `<resource_pack>/` link. The
[prompted demo](../demo/prompted/flow.md.html) frames one node differently for
its `fleet` and `construction` packs and adds a follow-up prompt only after the
first turn; `tests/integration/test_prompted_demo.py` runs it under both packs
with a controlled model service.

## Typed node Functions

A typed (State-declared) node may declare the same `### Functions` table as the
binding grammar, in its own `# Node:` document or in an inline `## Node:` block
of a component document. The table is optional; a node without one keeps its
current behavior. When present it has exactly one row whose `ID` is the node ID
and whose `Registry` is the node's `Native` registry. Columns and variants are
those of the binding grammar: four columns, seven columns that add
`Response schema | Output mode | Fallback`, or eight with `Display summary`.

```markdown
## Native

| Registry | Kind | Source |
| --- | --- | --- |
| decide | llm | [Python](native.py) |

## Functions

| ID | Registry | Input | Output | Response schema | Output mode | Fallback |
| --- | --- | --- | --- | --- | --- | --- |
| decide | decide | [input](decide_input.json) | [output](decide_output.json) | [response](decide_response.json) | structured | reject |
```

The shared checks apply unchanged: object schemas in JSON Schema draft 2020-12
without `$ref`, output modes `structured`, `json`, `prompted_json` or `text`,
fallback `allow` or `reject`, and the same Kind rule (a `deterministic` node
requires `-` in all three response columns; an `llm` node that declares response
columns requires an output mode). Linked schema files are captured as `schema`
resources, and their bytes join the workflow identity.

Typed semantics:

- **Input schema** validates the projection of the current State that the node
  reads: `{name: state[name] for name in input_schema['properties'] if name in state}`.
  Fields the schema does not list are not checked, `required` fields missing from
  State fail, and listed values must be JSON values.
- **Output schema** validates the partial State update the node returns, after
  the existing check that every returned key is a State field. Listed values
  must be JSON values.
- Property names in either schema must be State fields; a mismatch is rejected
  when the workflow is built.
- **Response schema, Output mode and Fallback** populate `context.response_contract`,
  so `context.response_request(messages)` works in typed `llm` nodes exactly as
  in the binding grammar. `Display summary` wraps the node's `complete` service
  in the same way.
- An Output schema violation in a node that declares an output mode raises
  `CandidateCorrection` (`reason: output_schema`). Typed graphs have no guarded
  components, so nothing repairs it: the error reaches the caller, as in the
  binding grammar when no guard exists. Other schema violations raise a plain
  `WorkflowError`.
- Compiled plans do not carry Functions, so compiling a typed graph that
  declares them is rejected.

## Named tools and optional dependencies

Workflows select named tool instances through linked Tools tables. Graph and
component tables declare availability; a consuming node's table also lists its
permitted operations. The runtime supplies `context.tools[port]` automatically
and rejects undeclared ports or operations before dispatch. Native code chooses
when to call the capability and supplies its arguments; it does not repeat the
instance binding. See [node capabilities](tools.md#node-capabilities) and the
[demo execution node](../demo/simple/workflows/components/execute_tool.md.html).

Tools can contribute more than a callable:

- **Configuration:** an implementation declares typed inputs, defaults and help;
  each instance binds these local slots to public configuration names. Only the
  selected implementation's consumed inputs become active. See
  [instance inputs](tools.md#bind-instance-inputs).
- **Development services:** an instance can declare optional local acquisition
  and recursive prerequisites through the shared infrastructure planner. For
  example, a tool can use a locally started API whose dependencies include a
  database. Compose assets, readiness, seeding and cleanup belong to those
  declarations, rather than arbitrary commands in the node. See
  [local services and recursive dependencies](tool-infrastructure.md).
- **Adapter dependencies:** selected implementations can declare captured Python
  dependencies and [host executable requirements](tool-executables.md).

Configuration and infrastructure are independent: a tool may need either, both
or neither. In the [bundled instance](../demo/simple/cfg/tools/primary.toml), a
blank `API_ENDPOINT` requests its declared local API; a nonempty remote endpoint
avoids that local closure. Separately declared unconditional prerequisites still
apply. Deployed agents can use externally managed endpoints without starting
development containers.

## Local verification

Use a Python environment installed with the project's development dependencies.
Run focused deterministic tests before any live authentication or inference:

`python -m api_agent.workflows validate` checks the root one-call example.
`python -m api_agent.workflows validate --resource-pack-dir demo` checks the
selected bundled API demonstration.
`python -m api_agent.workflows validate --demo contrived` checks the separate
qualification fixture. Add `--base-root PATH` to select either from another
checkout. For an external pack, use `validate --base-root PATH
--resource-pack-dir PACK` (or `diagram` in place of `validate`) to apply that
pack's declared graph and ordered replacements. `--entry PATH` is a raw
declaration check; it does not read a pack manifest or apply replacements.
The CLI validates and projects declarations without provider setup or graph
execution.

```sh
python -m pytest tests/unit/test_workflow_contrived.py tests/unit/test_workflow_sampling.py tests/unit/test_workflow_evidence.py --cov=api_agent.workflows.contrived --cov=api_agent.workflows.sampling --cov=api_agent.workflows.evidence --cov-branch --cov-fail-under=100
```

Complete the combined applicable 100% unit gate, installed-artifact checks and
independent review before selecting live integration tests. Live tests require
the standard integration provider/authentication configuration documented by the
repository and may incur provider usage.

## Deterministic workflow coverage

For standalone use, start `./launch.sh --tui-mode integration` from this
checkout. Its bundled `demo/` is selected automatically; save personal settings
in the ignored repository-root `.env`. For managed local report storage, add
`--allure-s3-use-localstack=True`, or save that same setting through Review
configuration. Derived bucket/endpoint defaults are valid for operation entry
without saving. Startup and leaving Clear .env offer pending-value review; menu
operations redirect only for actual configuration errors or missing active
requirements. Review shows the latest missing requirement in red above the
shortcut bar. Explicit-mode startup Review uses `c Close` to open the requested
picker with positional selectors preserved. Review required by a menu-selected operation also uses `c Close` to proceed
once its requirements are valid. Manual Review and Review after Clear .env use
`c Close / menu` to return to the main menu.

An external resource pack may instead own personal configuration and reports:
start `./launch.sh --resource-pack-dir <external-pack>`. In either case,
select the integration test identifiers below and use Continue only after the
gates above pass. These tests explicitly load the permanent contrived fixture,
independently of the selected pack's main workflow. Normal coverage requires no
statistical baseline, calibration refresh, skip or deselection. Historical pilots
and the explicit [maintenance procedure](workflow_calibration.md) remain separate.

Select normal coverage:

```text
tests/integration/test_workflow.py::test_workflow_node_coverage
```

Coverage draws fresh canonical version-4 UUIDs and uses the native fixture's
route/readiness decoder to discard combinations already exercised. It never
edits UUID bits. The ten reachable cases are two `skip` cases and four readiness
combinations for each of `next_a` and `next_b`. Rejected duplicate draws make no
LLM call. Each accepted UUID reaches the real graph unchanged, including its
prompts; the actual structured response, nodes, edges and required correction
attempts must pass validation before that case counts. Normal execution needs at
least 27 real completions; legitimate format repairs remain visible and costed.

`LLM_WORKFLOW_TEST_TIMEOUT_SECONDS` defaults to 600 seconds and covers provider
setup, catalog access, UUID selection and all ten graphs. Shorter parent deadlines
remain authoritative; existing owned cleanup drains separately. Provider profiles
may set the optional `LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS` cap. Application settings,
credential identity and ordinary model admission retain their existing behavior.

Structural evidence records actual attempts, guards, nodes, durations and routes.
Exact captured-resource hashes and owning commits remain in GRAPH journals for
historical joins. Additive declaration/settings digests are scoped diagnostics;
they do not replace exact source admission or prove arbitrary native/provider
behavior equivalent. See [identity requirements](prds/implementation_plan_b1c3a.md).
No live acceptance is established by deterministic unit tests alone.

## Controlled preference proof

The separate deterministic integration selection requires no provider fixtures:

```text
tests/integration/test_workflow_preferences.py::test_final_pass_relaxes_only_preference
tests/integration/test_workflow_preferences.py::test_truth_failure_stays_blocking_on_final_pass
```

Its checked-in `tests/integration/fixtures/workflow_preferences/flow.md.html`
hierarchy compiles through the actual declaration loader and workflow runtime and
matches registered function source files. Preference failure relaxes only on the
final pass; persistent truth failure exhausts the component. These tests publish
structural evidence. The permanent contrived model fixture retains strict guards
on every pass. Unit replay is `tests/unit/test_controlled_workflow_preferences.py`.

## Binding sources and session state

A binding grammar `## Bindings` row, `| Node | Input | From | Field | Default |`,
names where one node input comes from. `From` is `START` (the turn's input
object), an earlier node ID, or a reserved source: `PREVIOUS` (the node's output
from the last accepted pass), `FEEDBACK` (the guard's feedback), `ATTEMPT` and
`MAX_PASSES` (`Field` is `value`), `SESSION` or `TURN`. A `Default` other than `-`
supplies a JSON value when the source has no such field; `-` makes it required.

A root `## Session` table declares fields that survive across user turns:

```markdown
## Session

| Field | Source | Merge |
| --- | --- | --- |
| history | reply.messages | append |
| topic | reply.topic | replace |
```

- `Field` is a session field name (a Python identifier).
- `Source` is `node.output_field`. The node must exist and its `Output` schema
  must declare that property. Several rows may name the same field; rows for one
  field must use the same `Merge`.
- `Merge` is `replace` or `append`. `replace` keeps the value of the last source
  to run in flow order. `append` requires an array and extends the stored array;
  every source that produced output appends, in flow order. A source whose node
  did not run, or whose output omits an optional property, writes nothing.

A binding `| reply | history | SESSION | history | [] |` reads the field as it
stood at the start of the turn; values written earlier in the same turn come from
the writing node's ordinary binding. A missing field uses `Default`, and a `-`
default fails the node with a missing-session-field error. Loading checks that the
field is declared and, where both sides declare a JSON `type`, that the session
value type can satisfy the input schema. `SESSION` does not take `*`.

`Workflow.arun(update, thread_id=...)` requires a non-empty string `thread_id`
when a workflow declares `## Session`. The session is loaded from the checkpointer
at turn start and written back only after the whole turn succeeds, so a failed or
cancelled turn leaves it unchanged. Only the accepted pass of a guarded component
contributes: outputs from rejected passes are discarded before the turn ends.
Pass `checkpointer=` to `Workflow` (or a compiled definition's `workflow()`) to
persist beyond the process; the default is in-memory. A workflow without
`## Session` still rejects `thread_id` and `checkpointer`. Sessions are keyed by
`thread_id` alone, so use a distinct checkpointer per workflow. The table is part
of the declaration identity and compiled plans, and diagrams mark each node that
reads or writes a session field.

### Per-turn values with `TURN`

A binding `| finish | extra | TURN | extra | "none" |` reads output property
`extra` from whichever node most recently completed in the current turn with that
property in its output. It suits a shared step reached from several branches, which should read a
value whichever predecessor produced it. The value is the newest by completion
order, so a node that ran later overrides an earlier writer; a node that did not
run, or whose output omits the property, writes nothing. If no completed node has
written the field, `Default` applies, and `-` fails with
`Missing required turn value FIELD for node.input`.

Loading requires at least one node `Output` schema to declare `FIELD`, rejects `*`,
and checks that every declaring node's property type can satisfy the input schema
where both declare a `type`. Declaring nodes need not be earlier in the flow, so
the check does not constrain ordering.

Guarded components: the engine clears a component's node outputs when a new pass
starts, and TURN follows that. A value written only in a rejected pass is not
visible to any node in the next pass, nor after the component; if an earlier node
outside the component also wrote the field, that earlier value is what TURN returns
again. Use `PREVIOUS` and `FEEDBACK` to read prior-pass data deliberately.

TURN never sees an earlier turn; use `## Session` for that. The tracker is a
per-turn map from field to the nodes that wrote it, in completion order, resolved
against the node outputs that currently exist, so a node that runs again
contributes its latest output.

## Typed execution timeouts

The permanent qualification layout links graph, component and node timeouts to
`demo/cfg/config.toml`. `WORKFLOW_LLM_TIMEOUT_SECONDS` defaults to 120 seconds;
`WORKFLOW_GRAPH_TIMEOUT_SECONDS`, `WORKFLOW_COMPONENT_TIMEOUT_SECONDS` and
`WORKFLOW_NODE_TIMEOUT_SECONDS` remain unset by default. Each accepts a positive
finite float through ordinary configuration, environment or command-line options.
These optional settings do not add default acknowledgement prompts.

The selected API demonstration instead links its graph/component deadlines to
`demo/simple/cfg/config.toml`: 600 seconds for the graph and 300 seconds for a
component across its passes. Its LLM timeout is 120 seconds, and the guarded
component defaults to two passes. Child execution is clipped to the enclosing
deadline in both layouts.

A component deadline covers all of its passes. Every child budget is clipped to
the earliest enclosing deadline. The producer checks its LLM budget after the
call returns, so a provider swallowing cancellation cannot produce success after
its own timeout. Coverage records the resolved workflow budget identity; legacy
maintenance includes resolved settings in its historical sampling fingerprint. Unit tests cover typed defaults/environment/CLI,
linked numeric declarations, nested clipping, invalid values before inference and
swallowed cancellation; removing the post-call deadline check fails its test.

### Typed limits, component timeouts and expiry routing

A typed graph reuses the older grammar's configuration-backed budgets through
three optional tables. Tables with a `Source` link must name a `.toml`
configuration declaration; the same file may not also be declared as another
resource kind.

`## Limits` is allowed at the root and in a component declaration:

```markdown
## Limits

| Key | Source |
| --- | --- |
| GRAPH_MAX_PLAN_RETRIES | [config](cfg/config.toml) |
```

`Key` is an uppercase identifier, unique across the whole graph, naming an integer
field with a declared default. The effective value (`config.get(key, default)`)
must be an integer from 1 to 99 and is checked before any native source runs.
Declared keys become constants in typed `When` conditions, including prompt
selection: `.next_step == "plan_request" and .plan_retries <= GRAPH_MAX_PLAN_RETRIES`.
A condition that names an undeclared uppercase constant is rejected at load.
Effective limit values are part of the workflow semantic identity. Compiled plans
reject typed limits.

`## Timeouts` is now also allowed in a component declaration. A component row's
`Owner` must be that component or one of its direct children, and each owner has
one row across the whole graph. Enforcement is the same nested scope budget used
by root rows.

A row may add two optional columns that route an expiry instead of failing the turn:

```markdown
## Timeouts

| Owner | Source | Key | Expired | Expirations |
| --- | --- | --- | --- | --- |
| plan_request | [config](cfg/config.toml) | NODE_TIMEOUT_SECONDS | timed_out | timeout_retries |
```

`Expired` names a string State field and `Expirations` an integer State field
(checked when the `Workflow` is built; `-` in both columns disables routing for a
row, and `GRAPH` cannot route). When the node or component's own declared deadline
passes, the runtime ends that owner with the partial update
`{Expired: owner, Expirations: previous + 1}` and continues through the owner's
ordinary `When` edges, for example
`plan_request | plan_request | .timed_out == "plan_request" and .timeout_retries <= GRAPH_MAX_NODE_TIMEOUT_RETRIES`
with a fallback to an error node. A successful completion does not clear the
fields; packs clear them in their own node outputs. An ancestor's deadline, a
`TimeoutError` raised inside the node, and an owner that already dispatched a tool
or other effect still fail the turn. A routed expiry is recorded as a failed scope
end with `reason: declared_timeout_expired`. For a component, descendant work in
the expired pass is discarded and its open nested scopes are closed as failed.
Compiled plans reject expiry columns.

Calibration fingerprints also record the running Python version and installed
`langgraph`, `langchain-core`, `jsonschema`, `markdown-it-py` and `httpx` versions.
An explicit legacy calibration refresh binds its current dependencies; normal
coverage does not require a pilot after dependency upgrades. These are public package versions, without environment paths.

## Installed declaration check

Validate the root example with `python -m api_agent.workflows validate`, or the
selected demo with `python -m api_agent.workflows validate --resource-pack-dir demo`;
`diagram` prints declaration-derived topology without importing native bindings.
Build a wheel and install it into a separate environment. From an unrelated
working directory, run `python -m api_agent.workflows validate --demo contrived`
and `python -m api_agent.workflows diagram --demo contrived` with that
environment's Python. The packaged demo includes its linked functions, prompts,
schemas and typed configuration. Runtime consumers provide their own registered
functions and call the reusable `Workflow.arun` interface; the validation command
never imports declaration-selected code or performs inference.

`tests/unit/test_workflow_runner.py` proves real child-process structural event
forwarding in serial and parallel execution. `test_workflow_steps.py` verifies
nested Allure outcomes and rejects incomplete/cyclic scope ownership. The
separate deterministic preference integration tests exercise real compiled
execution without provider authentication.
