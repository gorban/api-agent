# Compiled workflow plans

A compiled workflow plan is canonical, versioned JSON generated from a selected
`.md.html` workflow declaration and its replacements. It lets production load
the real v2 or v3 engine graph without importing the declaration parser,
executing workflow source, or dynamically evaluating workflow source text.

## Guarantee and boundary

The guarantee is static: CI and the indexed pre-commit check recompile every
selected checked-in plan and require byte-for-byte equality with its sources.
The loader rejects incompatible plan schema versions, runtime ABI versions, and
workflow engine versions. It also validates canonical JSON and the
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

The canonical regeneration command is:

```bash
python -m api_agent.workflows.artifact_plans regenerate --root .
```

Use `python -m api_agent.workflows.artifact_plans check --root .` for the
same freshness check CI and pre-commit run.

## Demo production artifact

The bundled demo selects
`demo/.private_resources/plans/useful_api_demo.json`; its compiler input wheel
is retained under `demo/.private_resources/wheels/`.  The plan is the only demo
workflow representation copied into the production image.  Verify it without
the viewer toolchain:

```bash
uv run python -m api_agent.workflows.artifact_plans check --root .
```

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

Remaining gap: the local-only proof does not establish collector delivery or
production credentials.
