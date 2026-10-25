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

The implementation declares only its local inputs:

```toml
version = 1
interface = "verified-read@1"
factory = "factory.py:create_tool"

[inputs.ENDPOINT]
type = "str"
required = true
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

See the [bundled instance](../demo/simple/cfg/tools/primary.toml) and [infrastructure declaration](../demo/api/infrastructure.toml). A local output can satisfy a blank input provisionally; the factory receives a ready address only after the existing infrastructure owner verifies readiness and seeds. An instance's explicit `local.activation` supplies typed operation-only activation values for that local branch. Conflicting explicit settings reject. Disabled transitive dependencies remain errors. Acquired addresses never replace saved configuration.

Prerequisite closures use the shared planner and lifecycle, including compatible backing-service reuse and retryable cleanup. Configuration, in-process services and containers are independent: no endpoint, engine or object store is mandatory merely because an object is a tool. Factories receive only explicitly bound non-owning capability views of shared services, with disposal reserved for their parent.

The separately selected shared-primary fixture derives its secondary endpoint and headers from its primary and uses one acquired target. This is an explicit profile, not a global fallback. Ordinary independent targets remain independent; conflicting shared/independent use of the same instance in one selection rejects.

Agent-only deployments consume externally managed service addresses. Container isolation/platform qualification and deployment-image behavior retain their separate requirements; selecting a tool does not certify an isolation runtime or authorize deployment orchestration.
