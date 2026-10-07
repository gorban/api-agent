# Fresh repository skeleton and resource packs

This skeleton describes generic runtime and resource-pack responsibilities. Implement it using independently authored requirements and public upstream documentation.

The trees describe responsibility ownership and intended paths, not files to create immediately. Add files only as their selected feature is implemented. Pack/config `.yaml` names illustrate separate declarative documents; Layout uses restricted Markdown. Choose the remaining formats in the [declarative formats plan](implementation_plan_b.md). Python implementations and prompts stay together by responsibility and node; runtime prompt order comes from Layout declarations, not filenames.

## Declaration and prompt placement

Use a separate root `cfg/` tree for human-edited configuration, workflow Layout Markdown, and prompts. Shared settings are `api-agent/cfg/config.toml`; demo settings are `demo/cfg/config.toml`; resource-pack settings are `cfg/config.toml` under its pack root. Keep `pack.toml` at each pack root, and Python parsing/node implementations under `src/` or pack-owned code directories. This placement supersedes declaration/prompt locations in the illustrative trees below: mirror responsibility/node hierarchy under `cfg/workflows/`, with Layout-relative prompt links. Create workflow/prompt files only when their tasks are implemented.

## Checklist

- [x] Establish the initial packaging/source skeleton, shared `cfg/config.toml`, demo root pack with `demo/cfg/config.toml`, and packaged declaration loading. Human-reviewed foundation complete; later workflow/prompt/knowledge files remain uncreated until implemented.

- [ ] Establish this skeleton early, creating only the files needed for the [initial milestone](implementation_plan_e.md) and its prerequisites. Preserve existing instructions and planning documents; keep legacy deletions unstaged until final cleanup. Do not create empty placeholders for later workflows.
- [ ] Keep reusable runtime code in api-agent. Keep the included `demo/` directly under its repository root as an independently loadable resource pack. Support independently supplied resource packs selected by `--resource-pack-dir`, with each pack owning its configuration and launch integration.
- [ ] Give each pack a declarative identity/source manifest, configuration description, and Layout document. Share generic configuration declarations through a common schema rather than duplicating their definitions across packs; keep upstream API authentication implementation inside the selected extension, with pack-owned selection and UI strategy. Parse configuration at runtime for the initial milestone; introduce the general Layout interpreter in the later [Layout work](implementation_plan_b.md).
- [ ] Include the demo pack in packaging/distribution and resolve its installed location independently of the working directory. Resolve `<resource_pack>` to the selected pack root. Keep pack prompt components under responsibility/node directories, listed explicitly by the ordered Layout prompt links.
- [ ] Build an in-process graph snapshot from the selected pack's knowledge and navigation files using shared ingestion tooling; expose it as a retrieval/navigation tool inside the main Python agent process/container. Define an independently implemented runtime snapshot/cache and local search approach; do not require a Neo4j server or sidecar for this initial implementation. Keep checked-in sources separate from ignored derived artifacts. The graph is a tool/evidence store, not the LangGraph execution engine.
  - [ ] Define a backend-independent ingestion/retrieval interface. Initially use Python graph snapshots and local search; test startup/rebuild, atomic activation, concurrent reads and pack isolation without an external database. For multi-process deployments, define per-process rebuild/cache behavior explicitly. Defer Neo4j persistence, Cypher/vector indexes and any server lifecycle until separately planned; do not describe the in-process snapshot as embedded Neo4j.
  - [ ] Scope imports and queries by stable pack identity and snapshot identity. Track source paths/origins, hashes, freshness, and evidence authority; prevent data from different packs from mixing. Define the rebuild/update lifecycle and test pack isolation before using imported data in a workflow.
  - [ ] Keep checked-in GraphQL knowledge as navigation hints. Mark MCP-derived schema evidence separately, and use snapshots only while healthy/fresh. Live MCP evidence wins on conflicts or stale/unavailable snapshots. Preserve tool results separately from user prompt content. Add vector retrieval only where it improves navigation.
- [ ] Preserve generated `navigation/routes.json` relative to each pack root, its JSON contract, and any supplied generated navigation hierarchy. Adapt the importer to this output rather than reorganizing it. Knowledge and prompt files may use the new layout.

## Provider placement

Follow [provider architecture](provider_architecture.md) and [shared composition tasks](implementation_plan_m.md). Keep generic contracts/loading under src/api_agent/providers; put GraphQL-specific transport/schema/execution in api-providers/graphql-api-provider/src/graphql_api_provider, with its own cfg and tests. External LLM and upstream API implementations remain at explicitly configured external locations. The illustrative trees below do not make their GraphQL nodes or APIs mandatory in core; add only files needed by the current task. Generic knowledge ingestion accepts selected-provider evidence without requiring GraphQL.

## api-agent structure

```text
api-agent/
├── AGENTS.md
├── pyproject.toml
├── launch.py                              # Default demo pack + api-agent test ownership
├── docs/                                  # Existing implementation plans; generated docs separately
├── src/api_agent/
│   ├── __main__.py                        # Shared CLI entry point
│   ├── config/
│   │   ├── declarations.py                # Typed configuration declarations
│   │   └── resolve.py                     # Types, precedence, ENV/CLI, missing values
│   ├── runtime/
│   │   ├── resource_pack.py                # Discover selected pack and declared sources
│   │   ├── runner.py                       # Session inputs and execution lifecycle
│   │   ├── layout.py                       # Later: build LangGraph from Layout
│   │   ├── prerequisites.py                # Conditional service/profile selection
│   │   └── processes.py                    # Compose + host UI lifecycle
│   ├── auth/                             # Generic credential retrieval
│   │   ├── browser_login.py                # Provider-neutral browser-login test support
│   │   ├── credential_sources.py           # Named profiles and retrieval orchestration
│   │   └── password_managers/
│   │       ├── keeper.py
│   │       └── one_password.py
│   ├── providers/
│   │   ├── llm.py                          # Public LLM provider contract
│   │   └── load.py                         # Required provider location and config integration
│   ├── api/
│   │   └── agent.py                        # LLM discovery, chat and SSE; framework undecided
│   ├── tui/
│   │   ├── app.py                          # Textual views, configuration and mode selection
│   │   ├── config_cleanup.py               # Effective values and .env deletion
│   │   └── unattended_auth.py              # Credential-source setup wizard
│   ├── workflows/
│   │   ├── rendering/
│   │   │   └── renderer/
│   │   │       ├── node.py                 # Initial final renderer: Ok, no verifier/guard
│   │   │       └── prompts/
│   │   │           └── response_contract.md
│   │   ├── planning/                       # Later expansion examples
│   │   │   ├── planner/
│   │   │   │   ├── node.py
│   │   │   │   └── prompts/planning.md
│   │   │   └── plan_verifier/
│   │   │       ├── node.py
│   │   │       ├── conditions.py
│   │   │       └── prompts/verification.md
│   │   └── graphql/                        # Later expansion examples
│   │       ├── graphql_executor/
│   │       │   ├── node.py
│   │       │   └── prompts/execution.md
│   │       └── graphql_verifier/
│   │           ├── node.py
│   │           ├── conditions.py
│   │           └── prompts/verification.md
│   ├── tooling/
│   │   └── knowledge/
│   │       ├── ingest.py                    # Pack knowledge/navigation import
│   │       ├── graph.py                     # In-process graph snapshots, provenance and isolation
│   │       └── tool.py                      # Retrieval/navigation tool
│   ├── recording/
│   │   ├── session.py                       # User turns and provider call events
│   │   ├── usage.py                         # TOKEN_USAGE_CSV and cost estimates
│   │   └── quota.py                         # Suite snapshots and reset countdown
│   └── testing/
│       ├── runner.py                        # Selection, attempts, confirmation, cancellation
│       ├── browser_evidence.py              # Current-state all-tab Playwright failures
│       ├── allure.py                        # Integration/E2E labels and artifacts
│       └── history.py                       # Persistent S3 history; LocalStack locally
├── demo/                                    # Resource pack root, included in distribution
│   ├── pack.yaml                            # Illustrative: identity and source declarations
│   ├── config.yaml                          # Shared config schema + demo declarations
│   ├── layout.md                          # Later executable graph + ordered prompt links
│   ├── workflows/
│   │   ├── rendering/
│   │   │   └── renderer/
│   │   │       └── prompts/say_ok.md
│   │   └── planning/                        # Later expansion example
│   │       └── planner/
│   │           └── prompts/demo_context.md
│   ├── knowledge/
│   │   └── getting_started.yaml             # Meaningful demo knowledge sources
│   └── navigation/
│       └── routes.json                      # Preserve external generation contract
├── infrastructure/compose/                 # Independent reusable prerequisite stacks
│   ├── langfuse.yaml
│   └── localstack.yaml
├── tests/
│   ├── unit/                                # Deterministic; coverage; no Allure/retries
│   ├── integration/                         # Authenticated discovery, then chat/SSE
│   └── e2e/                                 # Human acceptance required per run
└── artifacts/                               # Ignored local results/recordings/generated docs
```

`workflows/<responsibility>/<node>/prompts/` is the navigation convention for generic code/prompts and pack-specific prompt components. Framework services such as authentication or recording have responsibility folders but do not need artificial LangGraph nodes. Tests can mirror these responsibilities within their test category as they grow. Packaging support files are omitted from the tree for readability.

The first implementation slice includes configuration resolution, LLM provider authentication, model discovery/provider wiring, recording/Allure, the API, Textual test controls, and `rendering/renderer`, plus the demo pack and generic resource-pack loading. General Layout execution, in-process graph ingestion, and planner/GraphQL examples expand in their own tasks; the skeleton does not make them prerequisites for the token-free model-listing test.

## Resource content contract

Knowledge sources contain stable IDs, titles, aliases/user wording, entity names, keywords, canonical GraphQL path hints, explanatory notes, and instructions to confirm against the live schema. Multiple files are supported. The new importer can accept an appropriate declarative knowledge format without retaining an old application's directory layout or treating those hints as authoritative schema.

Each resource pack documents its navigation data format, stable identifiers, route templates, and optional metadata. Preserve that documented contract during ingestion rather than requiring fields from a particular upstream application. Navigation hints can become graph relationships while the original generated files remain intact.

Pack descriptions also select test-owner repository identity/root and per-mode test paths. Unit mode always runs api-agent's unit suite plus any selected-pack unit tests, de-duplicating shared paths. Integration/E2E use only the selected pack's declared suites: the default demo points to api-agent tests, while selected resource packs use their own declared tests. The shared TUI/runner consumes that launch context; changing directories does not change which repository a launcher represents. Pack launchers locate the base runtime through the conditional `API_AGENT_DIR` setting documented in the [configuration plan](implementation_plan_b.md).

Extend this skeleton through the [conditional infrastructure/host UI plan](implementation_plan_h.md). Pack manifests declare their own API/database Compose fragments and may supply UI-project defaults.

Use [strict Markdown Layout](implementation_plan_b1.md) with genuine declaration-relative links for engine-local prompts. Pack prompt links retain `<resource_pack>` visibly and in their destination for runtime resolution; they do not resolve in editors/GitHub or point to a particular resource pack.

Document the public upstream interfaces and independently authored snapshot/cache and retrieval design used for this backend. Keep backend lifecycle and persistence requirements explicit in this plan.
