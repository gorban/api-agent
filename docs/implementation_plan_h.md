# Conditional infrastructure and host UI

Local API here means the pack's upstream API; the api-agent chat API remains the shared application. Compose filenames below describe responsibility boundaries, while selected pack declarations supply the concrete service definitions and startup requirements.

## Checklist

- [ ] Add a shared prerequisite planner and lifecycle manager driven by runtime-parsed config and pack declarations. Resolve enabled Compose fragments/profiles and host processes before starting the selected TUI action. Validate invalid combinations and missing active settings together; do not start containers for disabled features or deterministic unit tests.
- [ ] Separate independently selectable Compose profiles: `langfuse`, `api`, and `database`. Account for Neo4j and local S3/LocalStack as their own prerequisites. Support combinations rather than one monolithic all-services startup. Packs declare their API/database fragments; reusable stacks live in api-agent.
  - [ ] Support remote upstream API with no local API/database; local API using remote SQL Server; and local API using local seeded SQL Server. Langfuse may be independently enabled with any valid combination. Reject local database mode without local API mode, and do not ask for database connection configuration when using a remote API.
  - [ ] Assemble the selected Compose project with correct dependencies, readiness checks, named volumes, and distinct service/port names. Account for container-to-container and host-to-container endpoint differences. Validate supported combinations with `docker compose config`, including cross-profile dependencies; do not assume enabling one profile automatically enables another profile's services.
  - [ ] Track which containers/processes this invocation started versus reused. Expose startup status and logs in the shared TUI; on failure/cancellation stop owned processes safely and preserve reusable services and persistent data. Verify the existing Docker/Compose command contract with the local host-Podman implementation.
  - [ ] Expose resolved browser destinations and labeled live log streams to the [interactive local-run menus](implementation_plan_a.md). Apply named `RUN_LOG_SOURCE` selection (default `agent` interactively and `all` under `NO_STD_IN`); provide conditional log sources for launched prerequisites and UI, retaining component identity in the stream metadata so the all-logs view can label entries while single-source output remains raw. Browser destinations can include a configured remote UI/Langfuse, while log availability depends on an actual accessible local stream.
- [ ] Implement optional Langfuse tracing and its isolated local Compose stack, using the current official supported deployment rather than guessing its dependencies. Use typed `LANGFUSE_MODE` (`disabled`, `remote`, `local`) and conditional endpoint/key settings. Disabled mode needs no Langfuse credentials; local mode starts its full dependency stack and supports headless local project/key initialization.
  - [ ] Correlate LangGraph/provider traces with session, suite, test, and attempt identities. Flush pending traces on completion/interruption. Langfuse supplements Allure and token CSV reporting; it does not replace those histories. Cover disabled/enabled instrumentation deterministically before any live tracing check.
- [ ] Start the frontend as a host process, never as a container, for every executed E2E test and for local-run with `RUN_UI=True`. E2E with `RUN_UI=False` still uses the real UI through headless Playwright. Unit and integration tests never require or start the UI, regardless of `RUN_UI`. Declare `UI_SOURCE_DIR` and `UI_NX_APP` in shared configuration, with pack default overrides.
  - [ ] Require `UI_SOURCE_DIR` for E2E execution regardless of `RUN_UI`, and for local-run with `RUN_UI=True`. Anchor relative paths to the launcher/test-owner repository root, not the process working directory or the nested demo directory. Allow `.env`, environment, and CLI overrides such as `--ui-source-dir` and `--ui-nx-app`.
  - [ ] Resolve `UI_NX_APP` from shared configuration or selected-pack defaults, with explicit `.env`, environment, and CLI values taking precedence. Require a value when `RUN_UI` is enabled. Verify the configured Nx project/serve target and invoke the workspace's Nx runner from the configured checkout.
  - [ ] Wait for readiness, connect the UI to the resolved chat/upstream endpoints, expose process logs, and handle shutdown/cancellation through the shared process manager. Validate checkout/Nx settings only when starting UI. E2E always starts the real UI and waits for readiness; `RUN_UI=False` controls headless browser visibility, not whether the UI runs.
- [ ] Add deterministic tests for hierarchical defaults, enabled prerequisite combinations, path resolution, readiness/failure handling, and owned-process cancellation. Add targeted integration checks for selected local stacks and host UI; keep full E2E execution behind the existing human acceptance gate. These stacks are added as their selected functionality is implemented, not as new mandatory prerequisites for the token-free model-listing milestone.

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
    ├── neo4j.yaml                 # Knowledge graph tool infrastructure
    └── localstack.yaml            # Local S3 for persistent Allure history
```

Implementation references: [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/) support selecting multiple profiles; [Langfuse's official Compose deployment](https://langfuse.com/self-hosting/deployment/docker-compose) supplies its supported stack and headless initialization guidance.
