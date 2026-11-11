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
| Node | `Timeouts` | `Owner` (a present cell may also name the node's own `<node>.<port>`) |
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

A prompt is ordinary text (an inline `## Prompt:` block or a linked `.md`/`.txt`
file) that may contain `{{slot_name}}` placeholders. It carries no code and the
workflow declares no slot table: the selected resource pack supplies the text.
A pack lists its slots in its `pack.toml`, each mapping a slot name to a
pack-relative `.md` or `.txt` file:

```toml
[prompt_slots]
persona = "prompts/persona.md"
rules = "prompts/rules.md"
```

The same workflow runs under different packs by selecting a different pack root.
Rules:

- Slot names are lowercase identifiers (`[a-z][a-z0-9_]{0,63}`); anything else in
  braces, such as `{{ spaced }}` or `{{Upper}}`, is ordinary text.
- Each slot resolves independently, taking the first pack, in this order, whose
  `[prompt_slots]` supplies it: the **pack root** (the selected, consuming pack),
  then the **base root** (the library or owning workflow root, when it differs
  from the pack root). A library ships default text in a `pack.toml` at its base
  root; a product pack selecting the library workflow overrides any slot it
  declares and inherits the rest, and may supply slots the library lacks. This is
  the order in which `[[workflow.replacements]]` and `<resource_pack>/` links
  already prefer the pack root over the base root, and it applies the same way
  whether the product selects the library through `[workflow.library]` with an
  `## Import` wrapper in its pack or loads a base-root entry with its own pack.
  Each root's manifest is read only when a prompt needs a slot, and a root without
  a `pack.toml` supplies nothing.
- A used slot that no root supplies is a load error naming the slot, the `Prompts`
  row and every root consulted, in order (`node.md.html:26: prompt uses slot
  closing that no consulted resource pack supplies; consulted: pack /p, base /l`).
  A supplied slot no prompt uses is allowed and is not captured.
- Each used slot file is captured as a `prompt` resource, so its bytes and the
  resolved slot-to-file mapping join the workflow identity, the viewer projection
  and the compiled plan. The logical path starts with the root that supplied it
  (`pack/...` or `base/...`), so the same library workflow under two selecting
  packs has different identities when their slot text differs, and identical
  identities when the supplying root, path and text are equal. Slot files must be
  UTF-8.
- Slot text is the file's text without its single final line break, so a trailing
  newline added by an editor does not land mid-sentence. Filling is one pass over
  each prompt's own text at delivery to the node (`context.prompts`); supplied
  text is never rescanned, so a `{{...}}` inside slot text stays literal.
- Write `\{{name}}` for a literal `{{name}}`: a backslash directly before a
  placeholder is consumed and the placeholder is not filled or required.
- A compiled plan embeds the resolved slot texts (compile under the selecting
  pack to get its text) and does not read any pack at run time. Packs that are
  only ordered earlier or later on the launcher command line, and not the
  workflow's pack root or base root, are not consulted.
- The viewer's `prompts` references list the slot names they use, and each use
  adds a `prompt_slot` reference to the pack-supplied file.

[`tests/fixtures/prompt_slots`](../tests/fixtures/prompt_slots) holds one workflow
and two packs (`fleet`, `bakery`);
`tests/unit/test_binding_prompt_slots.py` runs the workflow under both with a
controlled model service.

Typed (State-declared) workflows still use the earlier `## Prompt Slots` table of
Python callables (see the [prompted demo](../demo/prompted/flow.md.html)); it is
kept only until the typed grammar is retired.

## Declaring the application

A binding-grammar root declaration may name the factory that builds its runtime
application with the same table the typed grammar uses:

```markdown
## Application

| Registry | Source |
| --- | --- |
| create_application | [Python](<resource_pack>/application.py) |
```

One row names a Python identifier and a `.py` source (a relative link or a
`<resource_pack>/` link). The source is captured as a `native_source` resource, so
its bytes join the workflow identity, the viewer references and the compiled plan
(which needs the source in a wheel, like any native source). The factory contract
is the [stateful application adapter](resource-packs.md#stateful-application-adapters)
contract: `create_application(definition, *, config)` returns an object retaining
that `definition` with an asynchronous `complete`. When the section is present
`selected_application` uses it; without it the bundled chat and useful-demo
applications are selected as before.

An application gets no tool access of its own. Tools are reached only through
`context.tools[port]` inside the workflow's nodes, bound to the running node and
limited to its declared ports and operations. Whether the selected API provider
is needed follows from the declared tool usages, and the chat request deadline is
the declared `GRAPH` row of `## Timeouts` resolved against the effective
configuration. A workflow without a `GRAPH` timeout value cannot be composed for
chat. An application may still expose `timeouts` or `requires_api`, but admission
rejects a value that disagrees with the declaration.

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
- `Merge` is `replace` or `append`, applied to completed node runs in completion
  order. `replace` keeps the value from the last completed run of any of the field's
  sources. `append` requires an array and extends the stored array with the value
  from every completed run of every source, so a node that looped twice appends
  twice. A run of one node applies its rows in declaration order. A source whose node
  did not run, or whose output omits an optional property, writes nothing. A run
  discarded by a rejected guarded pass, with everything else that pass produced,
  does not count.

A binding `| reply | history | SESSION | history | [] |` reads the field as it
stood at the start of the turn; values written earlier in the same turn come from
the writing node's ordinary binding. A missing field uses `Default`, and a `-`
default fails the node with a missing-session-field error. Loading checks that the
field is declared and, where both sides declare a JSON `type`, that the session
value type can satisfy the input schema. `SESSION` does not take `*`.

`Workflow.arun(update, thread_id=...)` requires a non-empty string `thread_id`
when a workflow declares `## Session`. The session is loaded from the checkpointer
at turn start and written back only when the turn's final outcome is passed (or an
outcome a `## Session Commit` table lists, below). A turn that raises, times out, is
cancelled, or ends with any other explicit `partial`, `failure`, `cancelled` or
`unknown` execution outcome leaves it unchanged. The write happens
inside the graph scope, under the graph timeout and before the journal is finalized
and published, so a slow or failing store fails the turn and is journaled as such.
If finalizing or publishing the journal then fails, the start-of-turn session is
written back before the error propagates, while the thread is still held. That
restore is a second write rather than a transaction: if it also fails, the stored
session is the failed turn's. Only the accepted pass of a guarded component
contributes: outputs from rejected passes are discarded before the turn ends.
Pass `checkpointer=` to `Workflow` (or a compiled definition's `workflow()`) to
persist beyond the process; the default is in-memory. A workflow without
`## Session` still rejects `thread_id` and `checkpointer`. A session is stored under
`<graph_id>:<thread_id>`, so graphs that share a checkpointer and thread names keep
separate sessions (graph IDs never contain `:`). Fields in a stored session that the
`## Session` table no longer declares are dropped when it loads and are not written
back. The table is part of the declaration identity and compiled plans, and diagrams
mark each node that reads or writes a session field.

### Declaring which outcomes commit the session

By default the session is saved only when the turn's final outcome is `passed`. A
root `## Session Commit` table widens that policy to turns an application labels
`partial`:

```markdown
## Session Commit

| Outcome |
| --- |
| passed |
| partial |
```

- `Outcome` is `passed` or `partial`. Rows are unique, and `passed` must be listed
  whenever the table exists, so the table only ever adds outcomes to the default and a
  turn that finishes normally keeps committing. `failed`, `cancelled`, `unknown`, a
  timeout or any other value is rejected at load with an error at the offending row,
  because those turns must leave the session untouched. An empty table, a repeated
  outcome, a table without `passed`, or a `## Session Commit` without a `## Session`
  is rejected the same way.
- A `partial` turn is one whose `execution_outcome` service returns the `partial`
  state. It commits exactly like a passed turn: same merge, same write inside the graph
  scope, same restore if journal finalization or publication then fails. A turn that
  raises, times out or is cancelled never commits, whatever the table lists.
- The table is part of the declaration identity, and of compiled plans, which decode
  it as strictly as `## Session`. A graph without the table keeps its previous identity.

`await workflow.session(thread_id)` returns a deep copy of the session committed for a
thread, without running a turn: `{}` when no turn has committed one, and only the
fields the `## Session` table declares now. It reads the same `<graph_id>:<thread_id>`
entry a turn loads, so a graph never sees another graph's session even through a shared
checkpointer. A workflow without `## Session` raises `WorkflowError`, and an empty or
non-string `thread_id` raises `ValueError`. Mutating the result changes nothing stored.
A turn still in flight has not committed yet, so the read returns the previous session.

A thread runs one turn at a time. Within one `Workflow` instance a second `arun` for a
thread whose turn is still running is refused at once with `RuntimeError` rather than
queued: two overlapping turns would start from the same stored session, and one would
overwrite the other's writes. The thread stays claimed until a failed turn's restore
has finished. The guard does not span separate `Workflow` instances sharing a
checkpointer, or processes; callers that run those against one thread must serialize
turns themselves, and the checkpointer is not a transaction across them.

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

### Tool call timeouts

Every timeout in a workflow run is a declared `Timeouts` row; the engine adds no
hidden per-call budget. A tool call (`context.tools[port]`, or `context.tool(port)`
for the earlier fixed-operation view) runs in the budget of the node that makes it,
clipped by its component and `GRAPH` deadlines. To cap one port, name it in the
`Owner` column as `<node>.<port>`:

```markdown
## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| first_draft.search | [configuration](cfg/config.toml) | WORKFLOW_SEARCH_TIMEOUT_SECONDS |
```

The row resolves like any other (positive finite seconds, or unset for no cap) and
caps that port's preparation and execution inside the node budget; the diagram shows
`Timeout: KEY` on the port's tool binding. Port rows load only in the bindings
grammar, where `Owner` is required for them: the node must exist, the port must
appear in that node's own `Tools` table, the key must be a configuration field,
and an owner may appear once. A node document names its own port with the full
`<node>.<port>`; the bare port is not an owner. Port rows are part of the
declaration and workflow identity and are checked again when a compiled plan loads.
Runtime lifecycle limits (starting and closing the tool runtime) stay outside the
workflow run and are not declared here.

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
must be an integer from 0 to 99 and is checked before any native source runs.
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

## Bounded loops in Bindings graphs

A Bindings-grammar graph declares explicit retry and replan loops with `## Limits`,
the `<step>.attempt` counter and backward `Edges` rows. Limits use the same table as
above in the root, a component or a node document; the declaring document is the
owner. A root limit is visible to every edge, and a component or node limit to edges
whose source is that step or inside it. Keys are unique across the graph and may not
repeat a component's linked `Max passes` key. The effective value is checked
(integer 0 to 99, configuration override over the declared default) before any
native source runs. A limit is an integer constant in `Edges` `When` conditions;
Bindings `Prompts` rows still name registered conditions and cannot read limits.

`<step_id>.attempt` evaluates to the number of times that node or component has
started in the current turn: 0 before its first start, 1 after the first run. Counters
reset every turn and count each start of a node (each pass of a guarded component starts
its nodes again) or entry of a component. They are engine state, so an `Output` schema may not declare a top-level property named `attempt`; that is
rejected at load. Bare `attempt` and `max_passes` keep their
guarded-component meaning.

An edge whose target is its source or earlier in Flow order (still a sibling) is allowed
only when its `When` is a conjunction containing `<target>.attempt < LIMIT` or
`<target>.attempt <= LIMIT`, where `LIMIT` is a declared limit key:

```markdown
## Limits

| Key | Source |
| --- | --- |
| PLAN_PASSES | [config](cfg/config.toml) |

## Edges

| From | To | When |
| --- | --- | --- |
| verify | next | verify.accepted |
| verify | draft | not verify.accepted and draft.attempt < PLAN_PASSES |
| verify | failed | otherwise |
```

With `PLAN_PASSES` of 2, `draft` runs at most twice; the `failed` route is an ordinary
edge. `<` allows LIMIT runs of the target and `<=` allows one more; a limit of 0 disables
the loop. `always`, `otherwise`, registered condition names, literal bounds, `or`, negated
or reversed comparisons are not bounds, and the edge is rejected with an error that
names the document declaring the row. A bound refers to a limit key, never a literal,
so the bound is visible configuration. A compiled plan is held to the same rule when it
loads: a plan whose backward edge has no such bound is rejected, and so is a plan with
an empty `Limits` table or a null `application_binding`, which the compiler never emits.
The same located errors apply to a bad `SESSION` or `TURN` binding declared in a
component or node document.

Re-entering a step re-runs it with fresh bindings, and each run's output replaces the
previous output in the turn's values, so later steps and conditions see the latest run.
Outputs of steps downstream of the target remain until those steps run again. Counters
span the whole turn: re-entering a component does not reset the counters of its
children. The graph's recursion limit is derived from the declared edges (each bounded
backward edge adds its limit value, plus one for `<=`, re-runs of every step), so a
turn with the largest declared loops still terminates normally. Journals of loops list
each visit as its own scope and edge and validate with the same strict route checks.
A journal's projection records the effective limit values and, on every bounded
backward edge, its limit key and whether it is `<=`. Validation counts each step's
starts in the journal and rejects (as incomplete, like an over-budget component pass
count) a traversal of a bounded edge once its target has started LIMIT times (LIMIT
plus one for `<=`). Callers that check a journal against an admitted definition pass
`expected_limits=` alongside `expected_component_passes=` when limits differ from
the declared defaults.
Limit rows and backward edges are part of the workflow identity and of compiled
plans, and the viewer shows each limit as `Configuration: KEY` in its declaring
diagram.

### Timeout expiry routing in Bindings graphs

A node or component with its own `## Timeouts` row (an `Owner` that is the step ID, not
`GRAPH` or `<node>.<port>`) can be routed on expiry by ordinary `Edges`, using the
engine-maintained Boolean `<step_id>.expired`. It is true while the step's latest run in
the current turn ended because its own declared deadline passed, and false before the
step first runs, after any run that ended otherwise, and at every restart of the step.
Like `attempt` it is reserved: an `Output` schema may not declare a top-level `expired`.
A condition that names `<step>.expired` for a step without its own `Timeouts` row is
rejected at load.

```markdown
## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| work | [config](cfg/config.toml) | WORK_TIMEOUT_SECONDS |

## Edges

| From | To | When |
| --- | --- | --- |
| work | work | work.expired and work.attempt < WORK_TIMEOUT_PASSES |
| work | timed_out | work.expired and work.attempt >= WORK_TIMEOUT_PASSES |
| work | next | otherwise |
```

If the step's own deadline expires and at least one of its outgoing edges reads
`<step>.expired`, the engine records the expiry and routes by the step's edges instead of
failing the turn:

- Only the edges that read the step's expiry are considered, and `always`/`otherwise`
  never apply to an expired run, so exactly one of those edges must match; if none does,
  the turn fails with the `TimeoutError` as it would without expiry edges. Put
  `<step>.expired` first in a conjunction, because the expired run's own output is absent.
- The run produced nothing: the step's output (and, for a component, the output of every
  step it owns) is removed from the turn's values and Session writes, so later bindings
  use their defaults or `TURN` fallbacks. Starts are counted, so `<step>.attempt` includes
  the expired run.
- A route back to the step is a backward edge and needs the usual bound, as above. The
  exhausted case is written explicitly (`>= LIMIT`) because edges must match exactly once.
- Only the step's own row counts. An enclosing component's or `GRAPH` deadline, a
  `TimeoutError` raised by native code before the step's deadline, and a tool port timeout
  inside the node propagate as before; a component with its own `.expired` edges is routed
  by those edges. Without a `.expired` edge the behavior is unchanged: the turn fails.
- A step that dispatched an effect (`operations@1` write, `mark_effect`) fails the turn
  instead of routing, so nothing is replayed or continued after an uncertain write. A
  component fails when any step it owns dispatched one.
- A node inside a guarded component is routed within the pass, and the guard and pass
  limits apply to what follows. A guarded component's expiry abandons all its passes and
  is routed by its own edges.

Outcome labels: the expired node's or component's scope ends `failed` with
`reason: declared_timeout_expired` (its progress ends inactive, its `scope_end` records
the declared and effective timeout), and the turn's outcome follows the final route. If
the routed path reaches `END`, the graph scope is `passed`, the Session is saved from the
runs that completed (the expired run contributes nothing), and an application's own
`execution_outcome` may still label the turn. A journal validates the route only when
that scope end names the step's own declared timeout (an ancestor-clipped budget does
not qualify), the edge taken reads the step's expiry, and every loop bound still holds;
a step that finished cannot take an edge that requires its expiry. The abandoned inner
routes of an expired component are closed with it.

Removed hidden budgets: the correction-authority and capture-authority calls that run
inside a node, and the provider-level inference budget of a provider without a
configuration field for it, now inherit the enclosing declared budget instead of a
hardcoded value. A provider's own configured inference timeout (for example a typed
configuration field with a default) remains a declared provider-level budget.

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
