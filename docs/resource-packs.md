# Resource packs and providers

## Run a resource pack

Use the pack's own launcher, or select it from api-agent:
```bash
./launch.sh --resource-pack-dir /path/to/pack
```

Repeat the option to compose packs in order:
```bash
./launch.sh --resource-pack-dir /path/to/shared-pack --resource-pack-dir /path/to/application-pack
```

Later pack declarations/defaults override earlier ones. Provider configs, suites,
production roots, dotenv files and infrastructure remain ordered contributions.
Relative path/provider defaults keep their contributing owner. An external pack
selection never inserts the demo pack. CLI/environment values retain their normal
precedence. The final pack owns output/configuration writes and service naming.

When a later pack redeclares `UI_PROVIDER` or `API_PROVIDER`, acknowledge that
replacement explicitly in its manifest:
```toml
[composition]
version = 1
replace = ["ui", "api"]
```

Select the implementations with existing typed fields or `[defaults]`. A single
earlier workflow/tool catalog is inherited. Competing workflow declarations need
`"workflow"` in `replace` and use existing workflow library/replacement contracts.
Multiple catalogs or differing workflow/catalog pack owners fail closed. See the
[composition requirements](prds/pack_composition.md) for scope and acceptance.

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

- LLM providers own authentication, sessions, inference and usage normalization. Their completion transports follow the [shared recovery contract](architecture/provider_contracts_v1.md#bounded-completion-recovery), including normalized HTTP evidence and disabled nested SDK retries.
- API providers own upstream access; the implementation can use GraphQL, REST
  or another protocol through the same consumer contract.
- UI providers own frontend startup configuration and browser authentication.
- Access adapters own application-specific authentication and identity checks.

Core orchestration consumes these interfaces rather than hardcoding a pack's
services. See [provider architecture](architecture/provider_architecture.md) and
[versioned contracts](architecture/provider_contracts_v1.md) for extension details.

## Tracing providers

Packs can select export-only or dashboard-capable tracing implementations through
the [tracing-provider guide](tracing-providers.md), using the same typed nested
configuration and lazy factory discovery as other providers. Optional local
collectors reuse shared infrastructure dependencies and leases.

## Output-token ceilings

`LLM_MAX_OUTPUT_TOKENS` defaults to 200,000 per LLM call, including reasoning
where the provider counts it. A pack uses this same shared setting; lower
call-specific budgets and advertised model maxima reduce the effective limit.
Transports without an output-limit capability omit the control.

A provider may raise `api_agent.providers.output_limits.OutputLimitRejected`
with `maximum_tokens` only after confirming a smaller output maximum was rejected
before inference. Shared execution permits one immediate corrected request under
the original deadline, separate from transport and workflow retry budgets.
Ambiguous failures and already generated/truncated responses do not qualify.
Learned limits expire according to `LLM_OUTPUT_LIMIT_CACHE_TTL_SECONDS` and stay
within the selected provider/account/model/route owner. Effective request capture
and physical attempts retain the correction. See the
[output-ceiling contract](prds/implementation_plan_f1d.md).

## Generated response contracts

A workflow can declare one generated response schema per node, separately from
its complete node output. Its native function explicitly constructs a
`CompletionRequest.response` policy through `NodeContext.response_request`.
Provider implementations resolve the requested mode for their actual route,
validate generated JSON against the original schema and report the actual mode.
Fallback permission changes formatting only; it never authorizes another
account, model, endpoint or a replay after failure. Legacy requests retain their
existing contract. See [declaration and binding requirements](prds/implementation_plan_f1a1.md)
and [included adapter modes and evidence](../src/api_agent/llm_providers/gpt/RESPONSE_MODES.md).

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

A pack with mixed unit and integration modules can declare
`tests.unit_exclude_markers = ["network"]` in its manifest. These explicit marker
identifiers affect only that owner's unit collection, including when composed;
they do not change other owners, integration/E2E selection or coverage targets.

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
For host language runtimes and CLI dependencies, see
[tools in another language](tool-executables.md).

The [independent-target fixture](../tests/integration/fixtures/tool_instances/independent/conversation.md.html)
binds `ENDPOINT` to `API_ENDPOINT` and `API_2_ENDPOINT` in distinct instances;
its secondary can select GraphQL or REST. The separate
[shared-primary fixture](../tests/integration/fixtures/tool_instances/shared_primary/conversation.md.html)
derives secondary values from the primary and acquires no second API stack.
See [fixture selection and inference-free checks](tools.md#inspect-the-delivered-examples).

## Provider-owned test defaults

LLM providers can contribute [exact-case integration profiles](integration_test_profiles.md)
for their qualified models, API formats, generation controls and deadlines. The
launcher resolves each test before creating its provider; application settings
and authentication identity remain unchanged. Explicit test controls take
precedence, and unsupported choices fail with guidance. This allows a provider
to use different qualified models for reasoning, Studio and structured workflows
without putting protocol branches in generic test drivers.

## Tracing extensions (planned)

Current tracing supports the built-in OTEL and LangFuse paths. A pack can
configure an OTLP-compatible destination, but cannot yet register a complete
new tracing implementation or managed dashboard through a provider contract.
Standalone `--trace-viewer` currently admits managed Jaeger and LangFuse only.

The [tracing-provider task](prds/implementation_plan_k1a2.md), grouped with
[GenAI field support](prds/implementation_plan_k1a.md), will add pack-owned export,
optional dashboard/login capabilities, nested configuration and optional recursive
local infrastructure through existing shared owners. Its documentation acceptance
includes a dedicated provider guide and examples linked from this overview.
See [current tracing configuration](tracing.md) for behavior available today.

For ordered packs, inherited auxiliary `[providers].configs` declarations must
use owner-independent defaults: absolute paths/selectors or base-root paths.
Relative path/selector defaults with pack/owner dependence, including conditional
defaults, are rejected. Put those defaults in the owning pack's main configuration
or use absolute auxiliary defaults until provider bootstrap supports per-file owners.

## Development dependency groups

A source resource pack does not need distribution metadata to declare test tools:

```toml
[dependency-groups]
dev = ["pytest>=8", {include-group = "checks"}]
checks = ["packaging>=24"]
```

Normal unit preparation selects `dev` through the shared environment helper.
Only admitted pack/base/library roots contribute; declarations do not activate
providers. Group names compare case insensitively with runs of `.`, `_` and `-`
treated alike. Includes resolve within their own manifest; missing includes,
cycles and colliding names fail. Only selected groups and their includes are
evaluated. A matching optional extra contributes additively, preserving existing
packs. Requirements use the existing PEP 508 and direct-URL validation policy.

A file listed in `[providers].configs` can opt into defaults for an exact selected
LLM provider without replacing its implementation or authentication configuration:

```toml
version = 1
[[provider_defaults]]
provider = "../../api-agent/src/api_agent/llm_providers/bedrock/factory.py:factory"
[provider_defaults.settings]
LLM_ALLOWED_MODELS = 'us[.]openai[.]gpt-(?:5|6)[.].+-luna'
```

The provider path is relative to the declaration file's directory, preserving its
owner when inherited by another pack. The canonical factory path and symbol must
match the selected LLM provider. Only `LLM_ALLOWED_MODELS`, `LLM_DEFAULT_MODEL`,
and `LLM_REASONING_EFFORT` are allowed; values retain the selected schema's types
and constraints. These explicit opt-ins replace default metadata after ordinary
provider declarations. Dotenv, environment, CLI and interactive choices remain
higher priority. Ordinary provider declarations still take precedence over
ordinary pack fields. The original declaration bytes participate in configuration
source tracking and ordered pack admission.

### Catalog-only workflow libraries

A tool catalog can resolve its `base` resources from a declared library without
selecting or relocating the application workflow:

```toml
[tooling]
version = 1
catalog = "workflow-catalog.toml"

[tooling.library]
source = "../shared-workflows"
distribution = "shared-workflows"
resource = "workflows"
```

The library uses the existing `workflow.library` ownership checks: an explicit
sibling source directory or one installed distribution with a checked file
inventory. Catalog graph loading uses this library; application loading keeps
its original workflow root and entry. Ordered packs retain the library with the
catalog's owner. Its static `pyproject.toml` dependencies and selected PEP 735
or optional dependency groups participate in ordinary dependency preparation.
Installed libraries require distribution ownership of that project declaration;
source libraries retain local-version and extra validation. No library code is
imported to discover requirements. A separate `workflow.library` can still own
the application.

Delegated workflow journals need a trusted exact-case graph association in the
catalog's `test_cases` table for frozen accounting to attach token facts to their
call leaves. The host derives admission from the selected graph definition and
that case's configuration before workers execute. A journal without matching
admission remains unverified and its accounting leaves remain unknown; journal
contents never establish their own admission.

## Stateful application adapters

A workflow can explicitly admit its application adapter alongside the state
schema and node bindings. Ordinary Run local requires this binding for a
stateful graph; workflow-only admission does not invent an HTTP adapter.

```markdown
## Application

| Registry | Source |
| --- | --- |
| create_application | [Python](application.py) |
```

The synchronous factory receives the admitted `definition` and resolved
`config`. Its object retains that same definition, provides asynchronous
`complete(request, *, llm, context)`, a `timeouts` mapping and a boolean
`requires_api`. The runtime supplies observation ownership and the selected
identity-bound LLM. Adapters can read the existing task-local execution and turn
context; they must not invent conversation identities from prompt text.
An optional asynchronous `aclose()` releases application-owned resources. Run
local retains it immediately after construction and closes it after the serving
operation drains, including when later startup fails. Unfinished disposal keeps
the ordinary cleanup receipt available for retry.

An optional `project_result(base_result)` callback enriches JSON display fields
at the HTTP boundary. It may replace `response_text`, but must preserve all
other standard result fields, including session identity and usage. Projection
receives an independent copy, must return valid JSON within the 1 MiB result
budget, and fails closed on invalid output. The framework calls the explicitly
selected callback; it does not interpret arbitrary model text as an application
envelope. The callback is also available to the selected UI composition through
the `workflow_application` service.

Admission derives the static import closure of every admitted native source,
including the selected owner's application module. A module that is loaded only
dynamically (for example by file path) can be added through captured TOML:

```markdown
## Native Dependency Manifests

| Source |
| --- |
| [TOML](<resource_pack>/application/dependencies.toml) |
```

Each manifest has `version = 1` and a `[modules]` mapping from Python module names
to source paths relative to that manifest. For example:

```toml
version = 1
[modules]
"example_adapter" = "../example_adapter.py"
```

Manifest bytes and all dependency source bytes participate in immutable
admission, snapshot size limits and provenance. Sources must remain inside the
declared roots and match their module paths; conflicting modules, malformed
manifests and changed admitted bytes fail before factory execution. This is an
explicit packaging closure, not permission to import unlisted local modules.
