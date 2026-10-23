# Application prerequisites, tracing and host UI wiring

[Infrastructure overview](implementation_plan_h.md) · [Prerequisite foundation](implementation_plan_h1.md).

## Checklist

- [ ] Add a shared prerequisite planner and lifecycle manager driven by runtime-parsed config and pack declarations. Resolve enabled Compose fragments/profiles and host processes before starting the selected TUI action. Validate invalid combinations and missing active settings together; do not start containers for disabled features or deterministic unit tests.
- [ ] Separate independently selectable Compose profiles: `langfuse`, `api`, and `database`. Account for local S3/LocalStack as its own prerequisite. The initial knowledge graph runs inside the main Python agent process/container through the [graph snapshot task](implementation_plan_g.md), with no additional Compose service. Any later Neo4j server backend requires a separately planned optional profile and lifecycle. Support combinations rather than one monolithic all-services startup. Resource packs define whether a deferred local upstream API is needed and declare any optional local database prerequisites. Packs declare their API/database fragments; reusable stacks live in api-agent.
  - [ ] Support remote upstream API with no local API/database; local API using an externally configured database; and local API using a selected locally seeded database. Langfuse may be independently enabled with any valid combination. Reject local database mode without local API mode, and do not ask for database connection configuration when using a remote API.
  - [ ] Assemble the selected Compose project with correct dependencies, readiness checks, named volumes, and distinct service/port names. Account for container-to-container and host-to-container endpoint differences. Validate supported combinations with `docker compose config`, including cross-profile dependencies; do not assume enabling one profile automatically enables another profile's services.
  - [ ] Track which containers/processes this invocation started versus reused. Expose startup status and logs in the shared TUI; on failure/cancellation stop owned processes safely and preserve reusable services and persistent data. Verify the existing Docker/Compose command contract with the local host-Podman implementation.
  - [ ] Expose resolved browser destinations and labeled live log streams to the [interactive local-run menus](implementation_plan_a.md). Apply named `RUN_LOG_SOURCE` selection (default `agent` interactively and `all` under `NO_STD_IN`); provide conditional log sources for launched prerequisites and UI, retaining component identity in the stream metadata so the all-logs view can label entries while single-source output remains raw. Browser destinations can include a configured remote UI/Langfuse, while log availability depends on an actual accessible local stream.
- [ ] Implement optional Langfuse tracing and its isolated local Compose stack, using the current official supported deployment rather than guessing its dependencies. Use shared `TRACE_MODE` (empty/omitted, `LangFuse`, `OTEL`) and conditionally defaulted `TRACE_TARGET`; infer local-stack startup from the selected backend and its local default target. See [tracing](implementation_plan_k.md). Disabled mode needs no Langfuse credentials; local mode starts its full dependency stack and supports headless local project/key initialization.
  - [ ] Correlate LangGraph/provider traces with session, suite, test, and attempt identities. Flush pending traces on completion/interruption. Langfuse supplements Allure and token CSV reporting; it does not replace those histories. Cover disabled/enabled instrumentation deterministically before any live tracing check.
  - [ ] Add the pre-"Ok" [LOG_CONTENT scrubbing gate](implementation_plan_j.md) to Langfuse and all tracing integrations. Unless the effective value is explicitly `True`, omit user/LLM/tool request and response content before emission/export, including automatic callbacks, failures, and flushes; retain only validated non-content metadata.
- [ ] Start the selected frontend as a host process, never as a container, for every executed E2E test and for local-run with `RUN_UI=True`. E2E with `RUN_UI=False` still uses the real UI through headless Playwright unless DEBUG inspection is enabled. Unit tests start no services; explicitly marked integration tests may require the selected prepared UI session. RUN_UI controls optional Run local UI, not test browser visibility. Providers own frontend checkout/project settings and may contribute `UI_SOURCE_DIR` and `UI_NX_APP` defaults where applicable.
  - [ ] Require `UI_SOURCE_DIR` for E2E execution regardless of `RUN_UI`, and for local-run with `RUN_UI=True`. Anchor relative paths to the launcher/test-owner repository root, not the process working directory or the nested demo directory. Allow `.env`, environment, and CLI overrides such as `--ui-source-dir` and `--ui-nx-app`.
  - [ ] Resolve `UI_NX_APP` from shared configuration or selected-pack defaults, with explicit `.env`, environment, and CLI values taking precedence. Require a value whenever the UI is created, including E2E with RUN_UI=False. Verify the configured Nx project/serve target and invoke the workspace's Nx runner from the configured checkout.
  - [ ] Wait for readiness, connect the UI to the resolved chat/upstream endpoints, expose process logs, and handle shutdown/cancellation through the shared process manager. Validate checkout/Nx settings only when starting UI. E2E always starts the configured host UI, even with `RUN_UI=False`, so Playwright can render responses and capture real Allure screenshots.
  - [ ] Define the UI renderer's proxy and environment configuration when wiring host UI startup. Supply the resolved chat, model-listing, and proxy targets and selected resource pack's test credentials through child-process configuration where needed, without changing the developer's frontend `.env` or reusing stale hardcoded credentials. Keep standalone demo LLM provider authentication free of resource-pack-specific API credential requirements. Verify the browser reaches this invocation's agent rather than a default remote endpoint or another checkout.
- [ ] Add deterministic tests for hierarchical defaults, enabled prerequisite combinations, path resolution, readiness/failure handling, and owned-process cancellation. Add targeted integration checks for selected local stacks and host UI; keep full E2E execution behind the existing human acceptance gate. These stacks are added as their selected functionality is implemented, not as new mandatory prerequisites for the token-free model-listing milestone.

## Provider-owned prerequisites

- [ ] Let selected API providers/packs declare any deferred local upstream API/database stack, endpoint/seeding configuration and readiness needs. Keep specific database engines, signing settings and service definitions in their owners. Integrate active fragments with shared ownership labels, leases, inventory and stop-time validation; collection starts nothing and no undeclared stack is inferred.
- [ ] Follow [browser-session policy](../architecture/provider_architecture.md) after UI readiness: automatic pack authentication uses the owned session, while a separate provider LLM session is created lazily. Run local prerequisites shut down on quit; retained test services remain scoped/observable through container cleanup.

## Skeleton additions

```text
api-agent/
├── src/api_agent/
│   ├── runtime/
│   │   ├── prerequisites.py       # Resolve active services and dependency combinations
│   │   └── processes.py           # Shared Compose/host lifecycle, readiness and logs
│   └── observability/
│       └── langfuse.py            # Optional trace integration
└── infrastructure/compose/
    ├── langfuse.yaml              # Profile plus all Langfuse dependencies
    └── localstack.yaml            # Local S3 for persistent Allure history
```

Implementation references: [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/) support selecting multiple profiles; [Langfuse's official Compose deployment](https://langfuse.com/self-hosting/deployment/docker-compose) supplies its supported stack and headless initialization guidance.

- [ ] Always start the configured host UI for E2E execution, even with `RUN_UI=False`. Integration starts a frontend only for explicitly declared UI-session requirements. Test UI browsers are headless unless DEBUG inspection is enabled; future E2E_DEBUG compatibility must share that same owner policy. RUN_UI controls only the optional Run local frontend. Required attended authentication and the bounded visible security-verification handoff remain exceptions. Collection starts no services.


## Local-run onboarding controls

- [x] Coordinate the user-guide naming with pack-owned hosting selection: RUN_UI enables the connected frontend; API_ENDPOINT uses a remote URL or an explicit blank for the pack's managed local upstream API. AGENT_DJANGO_URL selects the agent listener and must not trigger upstream provisioning. Do not introduce a competing shared RUN_API/API_MODE control. An unset endpoint retains its owning declaration's default, which can be remote.
- [ ] On local-run wiring, verify the README Get started path: configure RUN_UI and the selected pack/provider's API_ENDPOINT in Review configuration, choose Run local, acquire only declared enabled services, wait for readiness, and display their resolved endpoints/logs. Never infer an undeclared upstream API stack.

## Pack prerequisite conformance follow-through

- [x] Complete the [conditional contribution contract and synthetic pack proof](implementation_plan_o1.md) before dependent Run local and demo API consumers. The bounded Compose technical scope is delivered through the existing lifecycle foundation; human acceptance remains in its detail, and concrete database/service implementations remain separately owned. Generic host-process and Run local/UI wiring above remain open.

## Local operation consumer join

- [x] Deliver the [shared Run local runtime, frontend/browser and launcher join](implementation_plan_h1a1.md) on the accepted prerequisite foundation. Preserve the separately owned dashboard, product-service and application E2E gates.
