# Production runtime

The production ASGI entrypoint accepts an explicit checked-in compiled plan. Local
launches, tests, and the flow viewer continue to load resource-pack declarations
from source; `python -m api_agent.workflows.artifact_plans check --root .`
guarantees the selected checked-in plan is fresh.

## Declared tools

A plan that declares tools needs no production-only API setting. The entrypoint
admits the plan's tool instances with the source-run tool admission and binds
them from the process environment; for example the bundled demo requires
`API_ENDPOINT` and accepts `API_HEADERS`. Local demonstration fallbacks
(`[local]`, `[infrastructure]`) are not honored, so a blank binding fails
startup by field name. `API_AGENT_API_PROVIDER` is no longer read. See
`docs/compiled_plan.md` for the contract and its limits.

## Remaining gaps

Production graph selection currently uses `runtime.local_tools.selected_application`.
Any compiled graph with tool bindings is routed to `UsefulDemoApplication`, which
is a pre-existing demo-specific limitation. This slice does not redesign that
selection: private production packs must supply the lifecycle their compiled
graph requires.

## Network admission

The production entrypoint defaults to `127.0.0.1:8080`, loopback client
addresses, and the legacy `API_AGENT_CHAT_ORIGIN`. To bind a service-facing
address, set `API_AGENT_LISTEN_HOST` to an IP address and explicitly set all of:

- `API_AGENT_ALLOWED_HOSTS`: comma-separated exact Host names
- `API_AGENT_PERMITTED_BROWSER_ORIGINS`: comma-separated exact `http` or
  `https` browser origins
- `API_AGENT_CLIENT_ADDRESS_POLICY=any`

Wildcards, schemes and ports in Host values, malformed origins, and duplicate
entries fail startup. The chat bridge reflects CORS only for an admitted exact
origin and continues to require its session nonce. Local run and Studio retain
their loopback-only behavior.
