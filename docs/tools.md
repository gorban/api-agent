# Workflow-selected named tools

A workflow declares the tools it can use. An **interface** defines the callable contract, an **implementation** supplies its protocol and input schema, and a named **instance** binds those inputs to configuration. Reusing an instance from several nodes creates one operation-owned tool. Two different instance IDs retain different owners and verification grants even when their factories or endpoints match.

## Declare a port

A root, component or node can contain a Tools table:

```markdown
## Tools

| Port | Instance |
| --- | --- |
| primary | [primary](cfg/tools/primary.toml) |
```

See the actual [demo root Tools table](../demo/simple/flow.md.html#tools) and
[integration conversation Tools table](../tests/integration/fixtures/tool_instances/shared_primary/conversation.md.html#tools).
The repository's [default chat root](../flow.md.html) has no Tools table; tool
requirements come from the selected graph.

Ports resolve from the nearest lexical scope. A child cannot silently replace an inherited port with another instance. Replacements are applied before tool discovery. Discovery reads declarative resources without importing native nodes or tool factories.

A node borrows the selected instance through its execution context:

```python
from api_agent.workflows.tool_gate import ToolGate

gate = ToolGate(context.tool('primary'), invocation_id=call_id)
specification = await gate.describe()
prepared = await gate.prepare(arguments)
verified = await gate.verify(prepared, relevance=independent_verifier)
result = await gate.execute(verified)
```

The verifier must return both `relevant: true` and `grounded: true`. Owner validity runs first. Prepared calls and execution grants cannot be replayed through another gate. The operation owns disposal; a node cannot release sibling tools by closing a borrowed view.

## Bind instance inputs

An instance descriptor is versioned TOML:

```toml
version = 1
id = "example/primary"
interface = "verified-read@1"
selector = "PRIMARY_TOOL_IMPLEMENTATION"
implementation = "package:api-agent:api_agent/api_providers/graphql_mcp/tool.toml"

[bindings]
ENDPOINT = "API_ENDPOINT"
HEADERS = "API_HEADERS"
```

`selector` is optional. Its hidden string default selects the implementation descriptor; a normal typed configuration declaration can supply conditional defaults or explicitly expose that selector. Existing dotenv, environment, CLI and interactive precedence still applies. Another instance can use the same implementation with `ENDPOINT = "OTHER_API_URL"`; no configuration suffix is required.

`ENDPOINT` and `HEADERS` are implementation-local slots; `API_ENDPOINT` and
`API_HEADERS` are public configuration names chosen by this instance. The
factory receives local slot names, not all global settings. To add another
input, declare its type/default/help under `inputs` and bind that slot in every
instance that consumes it. For example, an implementation-specific path can
use `[inputs.PATH]` with `type = "str"` and `default = "/records"`, paired with
`PATH = "SECONDARY_PATH"` inside the instance's `[bindings]`. Only the selected
implementation's consumed slots contribute active configuration. Bindings for
an alternative implementation may remain dormant until it is selected.

The implementation declares only its local inputs:

```toml
version = 1
interface = "verified-read@1"
factory = "factory.py:create_tool"

[inputs.ENDPOINT]
type = "str"
required = true
nonempty = true
help = "Address of the selected service."
```

A factory is synchronous `create_tool(local_config, services)` and returns a version 1 owner with asynchronous `describe`, `validate`, `invoke` and `aclose` methods. `describe` returns the existing `ToolSpec`; validity and execution use `PreparedCall` and `ToolResult`. Inputs and prerequisites can each be empty. A compatible alternative must advertise its own real schemas; GraphQL arguments are never silently translated into another protocol.

Relative descriptor/factory paths stay inside admitted owners. `package:DISTRIBUTION:PACKAGE/PATH` names a distribution-owned resource whose installed receipt and content are verified. Installed missing assets do not fall back to a development checkout. Selected package sources, initializers, sibling modules and dependency requirements are captured before acquisition; execution validates the installed dependency identities and callable contract. The legacy GraphQL `create` entry translates `API_*` configuration only for its remaining API-provider consumers; `create_tool` accepts local slots, and mixed inputs reject.

## Select test graphs explicitly

A pack can declare:

```toml
[tooling]
version = 1
catalog = "cfg/tools/catalog.toml"
```

The catalog associates exact repository-qualified cases with graphs:

```toml
version = 1

[graphs.dual]
root = "pack"
entry = "tests/fixtures/dual/flow.md.html"

[test_cases]
"example::tests/integration/test_tools.py::test_dual" = ["dual"]
"example::tests/integration/test_plain.py::test_plain" = []
```

Graph entries may declare ordered replacements, finite variants and an isolated owner-relative resource root. Selected tests use only their declared graph union, never the application graph automatically. Freshly collected graphless identities retain independent authentication, UI and reporting requirements. An arbitrary unknown case string cannot add an execution closure. Workers receive the parent's captured plan and can narrow it to their case; they cannot discover another graph or implementation.

Review configuration uses the application at the main menu and the exact selected tests in the picker. Changing a case, selector or controller rebuilds membership. An alternate implementation adds only the inputs it consumes. Known inactive explicit CLI options produce a contextual error; saved/environment values stay dormant and remain available to Clear .env configuration. Hidden selectors and headers remain configurable explicitly without creating ordinary default-acknowledgement prompts.

A first launch prepares missing core parser dependencies through the normal managed Python environment before graph-aware configuration or help. Direct tool discovery stays inert: it does not install dependencies, import factories, start services or make network requests. The trusted test runner pins installed dependency identities after preparation and before dispatching workers.

## Prerequisites and shared services

See [optional infrastructure and cleanup](tool-infrastructure.md) for local output
bindings, recursive dependencies, Docker profiles and shared-primary behavior.
Infrastructure ownership is declared by the instance, separately from the
implementation's input schema.

## Inspect the delivered examples

The [independent graph](../tests/integration/fixtures/tool_instances/independent/flow.md.html)
links its [conversation and Tools table](../tests/integration/fixtures/tool_instances/independent/conversation.md.html#tools)
and [native implementation](../tests/integration/fixtures/tool_instances/independent_functions.py).
Its [primary](../tests/integration/fixtures/tool_instances/primary.toml) binds
`ENDPOINT`/`HEADERS` to `API_ENDPOINT`/`API_HEADERS`. Its
[independent secondary](../tests/integration/fixtures/tool_instances/independent_secondary.toml)
binds those slots to `API_2_ENDPOINT`/`API_2_HEADERS`, plus `PATH` to `API_2_PATH`.
The hidden `SECONDARY_IMPLEMENTATION` selector defaults to
[graphql.toml](../tests/integration/fixtures/tool_instances/graphql.toml).
Selecting [rest.toml](../tests/integration/fixtures/tool_instances/rest.toml) activates
`API_2_PATH`, defaulting to `/hello`; GraphQL leaves that setting inactive.
Both implementations advertise their own real callable schemas.

The normal [demo catalog](../demo/tool_catalog.toml) maps
[test_independent_tools](../tests/integration/test_tool_alternate.py) to
`independent_tools`; the [fixture catalog](../tests/integration/fixtures/tool_instances/catalog.toml)
maps the same exact case to `independent`. Rerun that one case with the REST
selector for alternate coverage. There are no two incompatible always-selected
GraphQL/REST cases.

The separate [test_shared_primary](../tests/integration/test_tool_instances.py)
selects the [shared-primary conversation](../tests/integration/fixtures/tool_instances/shared_primary/conversation.md.html)
and [secondary descriptor](../tests/integration/fixtures/tool_instances/secondary.toml),
deriving its endpoint/headers from the primary. Two owners and two verification
gates remain; this profile shares a target, not an owner. Default full test
selection is valid: the independent and shared-secondary instance identities
are distinct. An explicitly conflicting `API_2_ENDPOINT` or `API_2_HEADERS`
still rejects shared-primary selection instead of silently overriding intent.
Select the independent case alone when supplying different remote targets.
The backend is a synthetic API plus backing HTTP service, not a database implementation.

From a prepared source checkout, validate declarations without inference:

```bash
python -m api_agent.workflows validate --base-root . --entry demo/simple/flow.md.html
python -m api_agent.workflows validate --base-root . --entry tests/integration/fixtures/tool_instances/shared_primary/flow.md.html
python -m api_agent.workflows validate --base-root . --entry tests/integration/fixtures/tool_instances/independent/flow.md.html
./launch.py --tui-mode integration --collect-only --no-std-in api-agent::tests/integration/test_tool_alternate.py::test_independent_tools
```

Validation checks workflow declarations; collection discovers test identities.
These commands do not prove live tool execution or infrastructure readiness. Running
the integration test without `--collect-only` requires the ordinary execution
acceptance and configured LLM; it may launch the selected local closure and
spend tokens. To exercise the same independent case with its REST implementation,
use the normal integration execution gate explicitly:

```bash
./launch.py --tui-mode integration --no-std-in --start=True --debug=False \
  --secondary-implementation rest.toml \
  api-agent::tests/integration/test_tool_alternate.py::test_independent_tools
```

The delivered REST fixture is bounded; broader transport support, service-capability
combinations, platform isolation and durable storage retain their [separate delivery gates](prds/implementation_plan_o4.md).

### Nonempty string input policy

An implementation can declare `nonempty = true` on any string input. This rejects empty or whitespace-only effective remote values during configuration preflight, before prerequisites or factories start. The key must be a boolean and is invalid on non-string inputs. Omitting it or setting it false retains ordinary string semantics; `required` still controls presence independently.

An exact empty string remains valid when the instance declares a local output for that input: preflight retains its symbolic pending binding until acquisition supplies the effective value. Whitespace does not request local provisioning. Explicit shared-primary profiles derive their effective values and pending outputs before this check. Neither this policy nor the slot name implies a default endpoint or an undeclared prerequisite.
