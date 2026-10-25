# Resource packs and providers

## Run a resource pack

Use its own `launch.py`, or select it from api-agent:
```bash
./launch.py --resource-pack-dir /path/to/pack
```

A pack launcher locates the shared checkout through `API_AGENT_DIR`, normally
`../api-agent` relative to its root. .env, environment and CLI can override it.
Source launchers load that checkout directly without an editable installation.

## Executable descriptions

A pack's `pack.toml` declares its owner, configuration, test suites and default
provider selections. Its `cfg/config.toml` contributes typed settings/defaults;
selected providers add their own declarations. Python parsers remain in source;
human-editable configuration and workflow descriptions live under `cfg/`.

The [shared declaration](../cfg/config.toml) is the settings specification.
Do not duplicate its complete field inventory in prose or an .env.example.
Each field declares its type; conditions determine applicability, requirements
and defaults. Extensions preserve the shared field's type when overriding it.

Provider selectors use a `file.py:factory` location. Relative nested selections
respect the declaring owner. Discovery reads declarations without importing
factories, and rejects inconsistent/cyclic selection changes before execution.

## Replaceable responsibilities

- LLM providers own authentication, sessions, inference and usage normalization.
- API providers own upstream access; the implementation can use GraphQL, REST
  or another protocol through the same consumer contract.
- UI providers own frontend startup configuration and browser authentication.
- Access adapters own application-specific authentication and identity checks.

Core orchestration consumes these interfaces rather than hardcoding a pack's
services. See [provider architecture](architecture/provider_architecture.md) and
[versioned contracts](architecture/provider_contracts_v1.md) for extension details.

## Contribute integration tests

A selected provider can add adjacent `cfg/tests.toml`:
```toml
version = 1
[tests]
integration = ["tests/integration"]
```

Paths and symlinked content must remain inside the provider root. Keep
selection-dependent tests outside shared/pack suite roots, which are always
collected. Discovery deduplicates physical file/function/parameter identities.

Put generic integrations in api-agent, provider-specific assertions with their
provider, and application-specific assertions with the pack. Test parameters
should have readable IDs. See [configuration coverage](development.md#configuration-coverage)
for test-local overrides and scenario isolation.

## Workflow-selected named tools

A named tool separates an interface, a reusable implementation and a configured instance.
The [named-tool guide](tools.md) shows local input slots, public setting bindings,
implementation selectors and actual root/component Tools tables. The
[infrastructure guide](tool-infrastructure.md) covers optional Docker services,
recursive prerequisites, remote endpoints, sharing and cleanup.

The [independent-target fixture](../tests/integration/fixtures/tool_instances/independent/conversation.md.html)
binds `ENDPOINT` to `API_ENDPOINT` and `API_2_ENDPOINT` in distinct instances;
its secondary can select GraphQL or REST. The separate
[shared-primary fixture](../tests/integration/fixtures/tool_instances/shared_primary/conversation.md.html)
derives secondary values from the primary and acquires no second API stack.
See [fixture selection and inference-free checks](tools.md#inspect-the-delivered-examples).
