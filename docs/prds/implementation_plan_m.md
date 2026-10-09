# Shared provider contracts, selection and composition

Follow [provider architecture](../architecture/provider_architecture.md) for ownership and the approved browser contracts. This grouping covers generic core; the [GraphQL implementation](implementation_plan_m1.md), [upstream extension](implementation_plan_m2.md) and [LLM adapter](implementation_plan_f1.md) have separate implementation/test owners.

## Foundation already present

- [x] Provide explicit `LLM_PROVIDER` Python-file factory loading, integer version-1 validation, optional lifecycle/route hooks and neutral initialization errors in `src/api_agent/providers/loading.py`.
- [x] Discover the selected LLM provider's adjacent `cfg/config.toml` without importing its factory, and combine typed declarations before operation configuration. Keep help/configuration free of authentication/inference.
- [x] Honor pack-declared LLM factory defaults, owner-root-relative factory paths and ordered declarative `providers.configs` contributions. Present provider defaults as effective pack settings; preserve explicit value precedence and schema refresh after saves. Operation consumers, dependency setup and external adapters remain below.

These completed primitives do not complete the contracts or consumers below.

## Version-1 foundation checkpoint

- [x] Complete typed protocol/carrier exports in `providers/contracts.py`, operation capability checks and synthetic API/LLM/UI/upstream conformance without external protocol implementations.
- [x] Complete optional API/UI selection and nested declaration bootstrap, owner-relative imports, precedence/cycle rejection, deferred runtime startup, immutable services, route collision rollback and retained failed-load disposal. Check a regular streaming factory's async iterator when called; discovery must not invoke it.
- [x] Complete reusable scoped credential readers, coalesced browser preparation, lazy opaque LLM sessions, fresh getters, nested call tracking, process/loop ownership, bounded retryable disposal and explicit debug inspection retention in `providers/sessions.py`.
- [x] Independently review failure and cancellation assertions with focused mutations and join all applicable unit/coverage/build checks. The core suite passes 1,656 tests with exact 100% measured lines/branches; its wheel builds and installed exports load.
- [ ] Obtain human review of the concrete [contract document](../architecture/provider_contracts_v1.md) before external adapter/operation consumers proceed.

These primitives are separate from dependency installation, private adapter implementation, runner/TUI use, public operation completion and live acceptance. Do not mark the broader consumer tasks complete from synthetic owner checks. The surface is provisional until [consumer hardening](implementation_plan_m3.md) resolves normalized LLM data, activation, errors/deadlines, managed streams and graph-wide cleanup; that work precedes concrete consumer lanes and stable-contract human acceptance.

## Checklist

- [ ] Define versioned Python protocols and typed data/error contracts for API invocation, capability discovery, LLM discovery/completion/streaming, optional authentication, usage reads and cost estimation. Review the schemas before parallel implementations depend on them. Keep API payloads provider-defined; core must not require GraphQL query/variables/schema types.
- [ ] Implement planned `API_PROVIDER` and the included GraphQL provider's contributed `UPSTREAM_API_PROVIDER`, with no implicit default. Require LLM/API/upstream factories only for applicable executable operations. Help/configuration/unit collection and the renderer-only milestone must not require unrelated API setup.
- [ ] Extend provider bootstrap to include selected nested declarations before validating provider-specific CLI options. Define deterministic merge order, explicit default overrides and collision errors; apply base dotenv < pack dotenv < ENV < CLI. Recompute conditional defaults/applicability through TUI edits and reload after saves. Reject cycles and selection changes that leave executable factories bound to stale config.
- [ ] Anchor factory paths to their declared owner root and support provider-local package imports without global `sys.path` changes. Validate factory versions/capability signatures and ensure failed loads leave no executable module/session behind. Configuration discovery must not execute provider code.
- [ ] Integrate selected provider dependency groups with managed setup from either launcher; keep package/bootstrap manifests outside core-specific protocol assumptions. Prepare browser dependencies before operations, not in request handlers. Keep unrelated commands free of optional installs.
- [ ] Compose optional LLM/upstream `setup_routes` once per runtime before accepting requests, preserving core routes and rejecting method/path collisions. Give each handler immutable resolved configuration, verified TLS, scoped credential/authentication services and owned lifecycle registration. Route setup must work without successful login.
- [ ] Provide generic credential-role contributions and optional synchronized account index. Do not hardcode an index in bare core/demo; a provider with multiple accounts can contribute it without a secondary pack role. Validate only roles consumed by the selected operation. Connect the active schema to help/default review/cleanup and operation pickers.
- [ ] Implement the exact [browser/session contracts](../architecture/provider_architecture.md) through shared owners. Keep UI fixtures worker-local and LLM sessions backend-local; version any IPC requests without copying browser/getter objects across processes.
- [ ] Integrate optional upstream validation before execution; absent capabilities are allowed only when the selected contract declares them optional. Required validation cannot be bypassed through a direct transport/tool call. Keep identity relationships and signing inside the extension.
- [ ] Apply privacy registration before provider/child/route emissions, support provider-owned sensitive field/value declarations, and pass verified TLS contexts to outbound clients. Tokens/credentials remain excluded with `LOG_CONTENT=True` too.
- [ ] Drain calls, attempt final usage, dispose authentication/background work and close providers/clients in dependency order. Cover partial startup, failed rollback, cancellation while cleanup awaits, repeated close and configuration/account rebinding.
- [ ] Add synthetic conformance fixtures for interchangeable LLM adapters, included GraphQL plus an outside-path REST adapter, nested config, version/signature failures, route collisions, identity isolation, usage unavailable/zero/reset cases and lifetime failures. Use focused mutation checks when a validation test could pass for the wrong reason.
- [ ] Join core/provider builds and affected deterministic units. External providers own their protocol tests; public conformance tests require no external repository, registration, token format or real credentials. Present contracts and actual metrics for human review before live operations.
