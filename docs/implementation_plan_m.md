# Shared provider contracts, selection and composition

Follow [provider architecture](provider_architecture.md) for ownership and the approved browser contracts. This grouping covers generic core; the [GraphQL implementation](implementation_plan_m1.md), [upstream extension](implementation_plan_m2.md) and [LLM adapter](implementation_plan_f1.md) have separate implementation/test owners.

## Foundation already present

- [x] Provide explicit `LLM_PROVIDER` Python-file factory loading, integer version-1 validation, optional lifecycle/route hooks and neutral initialization errors in `src/api_agent/providers/loading.py`.
- [x] Discover the selected LLM provider's adjacent `cfg/config.toml` without importing its factory, and combine typed declarations before operation configuration. Keep help/configuration free of authentication/inference.

These completed primitives do not complete the contracts or consumers below.

## Checklist

- [ ] Define versioned Python protocols and typed data/error contracts for API invocation, capability discovery, LLM discovery/completion/streaming, optional authentication, usage reads and cost estimation. Review the schemas before parallel implementations depend on them. Keep API payloads provider-defined; core must not require GraphQL query/variables/schema types.
- [ ] Implement planned `API_PROVIDER` and the included GraphQL provider's contributed `UPSTREAM_API_PROVIDER`, with no implicit default. Require LLM/API/upstream factories only for applicable executable operations. Help/configuration/unit collection and the renderer-only milestone must not require unrelated API setup.
- [ ] Extend provider bootstrap to include selected nested declarations before validating provider-specific CLI options. Define deterministic merge order, explicit default overrides and collision errors; apply base dotenv < pack dotenv < ENV < CLI. Recompute conditional defaults/applicability through TUI edits and reload after saves. Reject cycles and selection changes that leave executable factories bound to stale config.
- [ ] Anchor factory paths to their declared owner root and support provider-local package imports without global `sys.path` changes. Validate factory versions/capability signatures and ensure failed loads leave no executable module/session behind. Configuration discovery must not execute provider code.
- [ ] Integrate selected provider dependency groups with managed setup from either launcher; keep package/bootstrap manifests outside core-specific protocol assumptions. Prepare browser dependencies before operations, not in request handlers. Keep unrelated commands free of optional installs.
- [ ] Compose optional LLM/upstream `setup_routes` once per runtime before accepting requests, preserving core routes and rejecting method/path collisions. Give each handler immutable resolved configuration, verified TLS, scoped credential/authentication services and owned lifecycle registration. Route setup must work without successful login.
- [ ] Provide generic credential-role contributions and optional synchronized account index. Do not hardcode an index in bare core/demo; a provider with multiple accounts can contribute it without a secondary pack role. Validate only roles consumed by the selected operation. Connect the active schema to help/default review/cleanup and operation pickers.
- [ ] Implement the exact [browser/session contracts](provider_architecture.md) through shared owners. Keep UI fixtures worker-local and LLM sessions backend-local; version any IPC requests without copying browser/getter objects across processes.
- [ ] Integrate optional upstream validation before execution; absent capabilities are allowed only when the selected contract declares them optional. Required validation cannot be bypassed through a direct transport/tool call. Keep identity relationships and signing inside the extension.
- [ ] Apply privacy registration before provider/child/route emissions, support provider-owned sensitive field/value declarations, and pass verified TLS contexts to outbound clients. Tokens/credentials remain excluded with `LOG_CONTENT=True` too.
- [ ] Drain calls, attempt final usage, dispose authentication/background work and close providers/clients in dependency order. Cover partial startup, failed rollback, cancellation while cleanup awaits, repeated close and configuration/account rebinding.
- [ ] Add synthetic conformance fixtures for interchangeable LLM adapters, included GraphQL plus an outside-path REST adapter, nested config, version/signature failures, route collisions, identity isolation, usage unavailable/zero/reset cases and lifetime failures. Use focused mutation checks when a validation test could pass for the wrong reason.
- [ ] Join core/provider builds and affected deterministic units. External providers own their protocol tests; public conformance tests require no external repository, registration, token format or real credentials. Present contracts and actual metrics for human review before live operations.
