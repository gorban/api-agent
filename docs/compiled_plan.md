# Compiled workflow plans

A compiled workflow plan is canonical, versioned JSON generated from a selected
`.md.html` workflow declaration and its replacements. It lets production load
the application graph without importing the declaration parser,
executing workflow source, or dynamically evaluating workflow source text.

There is one workflow engine and one declaration syntax: `# Workflow`, with
`# Component: id` and `# Node: id` below it. Declared state, bindings and
checkpoint continuation are features of that engine. Historical prototype
formats are rejected. Execution requires an admitted source definition or a
compiled plan; a layout projection alone is for inspection.

## Guarantee and boundary

The guarantee is static: CI and the indexed pre-commit check recompile every
selected checked-in plan and require byte-for-byte equality with its sources.
The loader rejects incompatible plan schema versions, runtime ABI versions, and
workflow contracts. It also validates canonical JSON and the
`compiled_plan_identity` over the encoded graph.

Plans record the plan digest, source hashes, and bound-module hashes for
provenance in recordings and traces. They are diagnostic data only. Runtime
does not enforce digests or source/module hashes, verify installed wheels or
RECORD files, pin imports, intercept imports, or reverify modules per workflow
or turn. Image tampering is outside this PoC's scope. Bound modules use normal
Python imports and must be available through the application's ordinary
production dependency composition.

`definition_id` continues to be derived from the logical source-mode graph
resources and replacements, so a freshly compiled plan has the same definition
identity as its declaration mode counterpart.

A binding-grammar plan adds two optional top-level sections only when its
workflow uses the feature, so other plans keep their bytes: `application_binding`
(the declared `## Application` registry and a bound native source) and
`prompt_slots` (each used slot name and the logical path of its pack-supplied
`prompt` resource). Slot paths join `definition_id`; the loader requires every
slot to name a UTF-8 prompt resource and the slot set to equal the placeholders
the plan's prompts use.

## Declarations a plan cannot carry yet

The compiler rejects these declarations, so a workflow using them runs only in
declaration mode and cannot reach the production entrypoint:

- typed-state node prompts and their prompt slots (binding-grammar
  `prompt_slots` above are supported);
- helper native sources (declare them as native dependencies instead);
- typed-state `Functions`;
- typed-state limits, and timeout-expiry routing in any workflow.

## Canonical encoding and order

The compiler emits identity-free mappings, including selected declarations,
declaration fragments, dependencies and declared resources, in lexical key
order. This keeps equivalent declarations byte-identical despite their parser
or mapping insertion order.

Sequences retain their declared order when it carries meaning: table, node and
edge rows are evaluated or displayed in that order. JSON schemas and other
resource content are retained as source bytes, so their property order is also
preserved for consumers such as LLMs.

## Checked-in location and regeneration

For a resource pack declaring `[workflow.artifact]`, its plan belongs at:

```
<pack>/.private_resources/plans/<graph_id>.json
```

This mirrors the generated workflow-view location under
`.private_resources`. The artifact declaration lists that plan, its graph ID,
and the source wheels used by the deterministic compiler:

```toml
[workflow.artifact]
plan = ".private_resources/plans/example.json"
graph_id = "example"
wheels = ["build/example-bindings.whl"]
```

First rebuild the demo compiler input after editing its bindings:

```bash
python scripts/build_demo_bindings_wheel.py
```

The canonical regeneration command is:

```bash
python -m api_agent.workflows.artifact_plans regenerate --root .
```

The production image qualifier's `qualification/simple_chat.json` is not a
resource-pack plan; it is a fresh compile of the root workflow. The same `check`
and `regenerate` commands cover it, so a stale copy fails the pre-commit check.

Use `python -m api_agent.workflows.artifact_plans check --root .` for the
same freshness check CI and pre-commit run.

A plan file is the compiler's exact bytes, with no trailing newline. Exclude
plan files from end-of-file fixers and mark them `-text` in `.gitattributes`
(this repository marks `**/.private_resources/plans/*.json` and
`qualification/*.json`); otherwise the freshness check and the loader both
report the added line ending.

## Demo production artifact

The bundled demo selects
`demo/.private_resources/plans/useful_api_demo.json`; its compiler input wheel
is retained under `demo/.private_resources/wheels/`.  The plan is the only demo
workflow representation copied into the production image, and it runs there
through the tool the `demo/simple` flow declares (see Declared tools below).
Verify it without the viewer toolchain:

```bash
uv run python -m api_agent.workflows.artifact_plans check --root .
```

## Declared tools

A plan carries its flow's `## Tools` declarations exactly as source mode reads
them: each tool usage (port, scope, operations) and the bytes of every tool
instance descriptor, which hold the instance identity, interface,
implementation reference, `[bindings]` and `[defaults]`. Nothing about a
tool is selected through a production-only setting: there is no separate
API provider selection, and `API_AGENT_API_PROVIDER` is not read.

At startup the production entrypoint admits those declarations with the same
tool admission and binding code as a source run (`compose_tools`, then the
ordinary configuration resolution and `bind_values`). The descriptor's binding
names become configuration fields, so the deployment supplies each value
under the name the declaration binds it to. For the demo's GraphQL tool
those are `API_ENDPOINT` (required, nonempty) and `API_HEADERS` (a JSON object,
default `{}`); the endpoint reaches the tool through the declaration's
`ENDPOINT` binding, not through a field the production schema declares.

Two limits follow from a plan having no source tree:

- A compiled tool instance must name a distribution-owned implementation
  (`package:<distribution>:<path>`). The production image installs the
  runtime as the `api-agent` distribution so that `package:api-agent:...`
  references resolve to the installed, receipted files.
- Production does not honor an instance's `[local]` and `[infrastructure]`
  tables. They start a local demonstration service for source runs and are a
  development convenience. A binding that resolves to a blank value fails
  startup with the field name, for example
  `API_ENDPOINT: selected tool input requires a nonempty string`, instead of
  falling back to a local service.

## Noninteractive runtime and image

`python -m api_agent.runtime.production` is the production entrypoint.  It
does not invoke the bootstrap, TUI, browser, viewer, local UI, dependency
manager, or test runner.  It binds the existing `/app/agent` API through the
same Studio backend composition used by local run, loads its configured plan
once on ASGI startup. Its default listener and browser admission remain
loopback-only, while an explicitly configured deployment can bind behind a
service or ingress.

Set `API_AGENT_PRODUCTION_PLAN`, `API_AGENT_LLM_PROVIDER`,
`API_AGENT_CHAT_ORIGIN`, and `API_AGENT_SESSION_NONCE`; optional values include
`API_AGENT_LISTEN_HOST` (default `127.0.0.1`),
`API_AGENT_LISTEN_PORT` (default `8080`), and
`API_AGENT_SHUTDOWN_TIMEOUT_SECONDS` (default `10`).  Development `DEBUG` and
`RUN_UI` values are ignored by this entrypoint.  Tracing remains off unless
`TRACE_EXPORT_ENDPOINT` is set; that opt-in selects the bundled OTLP adapter.
`TRACE_EXPORT_HEADERS`, `TRACE_EXPORT_PROTOCOL`, and
`TRACE_HTTP_DIAGNOSTICS_LEVEL=info` use the normal typed tracing configuration.

For a non-loopback `API_AGENT_LISTEN_HOST`, also set
`API_AGENT_ALLOWED_HOSTS` to comma-separated exact Host names,
`API_AGENT_PERMITTED_BROWSER_ORIGINS` to comma-separated exact `http`/`https`
browser origins, and `API_AGENT_CLIENT_ADDRESS_POLICY=any`. Wildcard hosts and
origins are rejected. The loopback defaults are retained when those settings
are omitted; local run and Studio do not use this production-only ingress
configuration.

Build the stripped runtime wheel and a local image without publishing:

```bash
uv run python scripts/build_production_wheel.py
docker build --no-cache -t api-agent-runtime:local .
uv run python scripts/qualify_production_image.py \
  --evidence artifacts/image-qualification
```

The qualifier saves the exact image digest and source commit, layer and
flattened filesystem inspection, Python distribution and Debian package
version/license inventories (with unknown licenses marked explicitly), and
deterministic host-to-published-port HTTP transcripts for readiness, models,
chat, stream, controlled error, cancellation, rejected unconfigured Host and
Origin requests, absent viewer/debug routes, and bounded SIGTERM cleanup. With
`TRACE_EXPORT_ENDPOINT` configured, the production
image installs the locked `tracing` extra and resolves the bundled OTLP adapter
through the normal typed configuration path.  The qualifier points it at a
refusing loopback endpoint, records the real delivery-failure warning, and
proves shutdown remains bounded.  The qualification-only image adds the
deterministic provider without adding test tooling to the production image.

A startup failure (an unreadable plan, invalid configuration, or a
composition error such as a missing `GRAPH` timeout) is logged on stderr as a
failure category and in-package stack locations, without exception text,
before the ASGI server receives `runtime_start_failed`. A configuration
error (for example a blank tool binding) also logs its message, which names
fields and fixed reasons and never values.

The image ships the two public providers that need more than the Python
runtime:

- **Bedrock** (`LLM_PROVIDER`). The image installs the locked `bedrock` extra,
  and the process helpers its worker uses live in the shipped
  `api_agent.runtime.processes` module rather than the excluded `testing`
  package. Select it by the installed file location, for example
  `API_AGENT_LLM_PROVIDER=/opt/api-agent/venv/lib/python3.13/site-packages/api_agent/llm_providers/bedrock/factory.py:factory`.
  Credentials come from `BEDROCK_SESSION_PROVIDER`. An explicitly empty value
  uses the SDK's already-authenticated profile or credential chain. A session
  adapter selected by file location can supply credentials instead. Set it
  explicitly for a deployment, because the resolved default is the attended
  sign-in adapter.
- **GraphQL MCP** (a declared tool). The image installs the locked `graphql`
  extra, a pinned Node runtime (checksummed in `mise.lock`) at
  `/opt/api-agent/bin/node`, and the provider's locked npm tree, prepared at
  build time into `/opt/api-agent/graphql-mcp`. The flow declares the tool
  with an instance whose implementation is
  `package:api-agent:api_agent/api_providers/graphql_mcp/tool.toml`, and
  production admits it as described under Declared tools. The image sets
  `GRAPHQL_MCP_ASSETS_DIR` to that directory, so the provider finds its assets
  whichever directory holds the plan or `API_AGENT_OWNER_ROOT` names. A set
  directory is verified and never prepared at startup: an incomplete or
  modified tree fails closed with `assets_missing`, `assets_invalid` or
  `assets_integrity`. Without the setting, the provider reads
  `<owner root>/.api-agent/graphql-mcp` and prepares it with `npm` on first
  start, which the image does not ship.

`tests/unit/test_production_wheel.py` proves the wheel carries every file of
both provider packages and the Node asset locks, and that an installed copy
imports both packages without `testing`, `tui` or `ui`. It also checks that the
image's `--extra` list covers each provider's declared requirements. The build
fails if either provider's import closure reaches an excluded module. The image
qualifier allows the `node_modules` layout only for the GraphQL MCP license
notices and its prepared asset tree.

The qualifier also runs the bundled demo plan in the qualification image. It
compiles `demo/simple` afresh and requires the image's plan to equal it, then
starts the plan with the deterministic provider (which answers the flow's plan,
review and render calls without inference) and a loopback stub GraphQL endpoint
inside the container, so nothing leaves the host. It requires the chat answer to
carry the stub's data, the stub to have received both the tool's schema
introspection and the planned query with the configured headers, and a start
without `API_ENDPOINT` to exit naming that field.

Remaining gaps: the local-only proof does not establish collector delivery or
production credentials. The two bundled providers were exercised inside a built
image without network access: the GraphQL MCP tool is now exercised by the
qualifier as above, while the Bedrock provider constructs and fails at its
credential step but is not yet repeated by the qualifier. The image's `graphql`
extra also installs the Python `mcp` package, which no shipped code imports.
