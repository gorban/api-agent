# Named-tool infrastructure and cleanup

[Named-tool contracts](tools.md) · [Resource packs](resource-packs.md)

## Optional local acquisition

See the [bundled instance](../demo/simple/cfg/tools/primary.toml) and [infrastructure declaration](../demo/api/infrastructure.toml). A local output can satisfy a blank input provisionally; the factory receives a ready address only after the existing infrastructure owner verifies readiness and seeds. An instance's explicit `local.activation` supplies typed operation-only activation values for that local branch. Conflicting explicit settings reject. Disabled transitive dependencies remain errors. Acquired addresses never replace saved configuration.

Prerequisite closures use the shared planner and lifecycle, including compatible backing-service reuse and retryable cleanup. Configuration, in-process services and containers are independent: no endpoint, engine or object store is mandatory merely because an object is a tool. Factories receive only explicitly bound non-owning capability views of shared services, with disposal reserved for their parent.

The separately selected [shared-primary fixture](../tests/integration/fixtures/tool_instances/shared_primary/conversation.md.html) derives its secondary endpoint and headers from its primary and uses one acquired target. This is an explicit profile, not a global fallback. The [independent fixture](../tests/integration/fixtures/tool_instances/independent/conversation.md.html) instead uses distinct primary and secondary outputs. Ordinary independent targets remain independent; conflicting shared/independent use of the same instance in one selection rejects.

Agent-only deployments consume externally managed service addresses. Container isolation/platform qualification and deployment-image behavior retain their separate requirements; selecting a tool does not certify an isolation runtime or authorize deployment orchestration.

## Retained services and last-use cleanup

Application and test graphs can select different services in the same managed project. A pack may explicitly register additional identity-only sources in `pack.toml`:

```toml
[infrastructure]
version = 1
declarations = ["infrastructure.toml"]
inventory = [
  { owner = "example", root = "pack", directory = "tests/fixtures", declarations = ["infrastructure.toml"] }
]
```

`root` selects the admitted base or pack anchor. Optional `directory` defaults to `.` and selects an existing canonical subdirectory of that anchor; declarations are relative to that owner directory. Symlinks, traversal and conflicting roots for the same owner reject.

An inventory row reads the declaration's owner and service identities, without reading its Compose fragments/assets, importing its factory or activating its settings. The exact declared service/profile pairs allow recognized, unselected containers to remain untouched when switching operations. Unknown or mismatched ownership still rejects. Inventory recognition does not grant endpoints, leases, logs or cleanup access.

Operations share compatible selected services through locked leases. If a creator finishes while another operation still uses its containers, eligible cleanup responsibility transfers to a remaining consumer. Cleanup runs after the last relevant lease releases, subject to explicit retention and volume-preservation policy. A failed cleanup remains owned for retry; unrelated retained services are never added to that responsibility.

## Recursive dependencies and Docker profiles

The bundled [instance](../demo/simple/cfg/tools/primary.toml) declares its local
output separately from its `[bindings]`:

```toml
[local]
slot = "ENDPOINT"
output = "demo/api/graphql"
path = "/graphql"
declaration = "../../../api/infrastructure.toml"

[local.activation]
DEMO_API_LOCAL = true

[infrastructure]
owner = "demo"
root = "../../.."
declarations = ["api/infrastructure.toml"]
```

These paths are relative to that descriptor and its admitted owner. Copying
this snippet into a different directory requires adjusting paths. The
[infrastructure declaration](../demo/api/infrastructure.toml) names services,
Compose fragments, dependencies, readiness and endpoints. The planner derives
owned Docker Compose service/profile names; a tool implementation does not
supply arbitrary Docker command lines or a `profiles` key in its input schema.
Use instance `prerequisites = ["owner/service"]` for unconditional required roots;
local-output roots apply only when their bound input requests local acquisition.

A nonempty remote endpoint supplies the input directly and avoids acquiring
that local output's closure. It does not suppress separately declared
unconditional prerequisites. An exact blank requests the declared local output;
absence without an applicable default and whitespace are not equivalent local
requests. Run local and selected integration tests may therefore start services
that a remote-only operation does not need.

The [fixture declaration](../tests/integration/fixtures/tool_instances/infrastructure.toml)
shows `fixture/primary` depending on `fixture/backing`. A pack could similarly
declare an API depending on a database with `dependencies = ["example/database"]`,
provided that service is declared with valid Compose assets, readiness and any
seed completion contract. Recursive planning starts prerequisites before their
consumers, rejects missing/cyclic or disabled dependencies and waits for
readiness/seed completion before handing endpoints to factories. The fixture's
backing service illustrates that dependency join; it does not qualify a real
database or durable-storage deployment.
