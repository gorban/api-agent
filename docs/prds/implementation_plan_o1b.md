# Infrastructure v1 declaration format

[Approved contract](implementation_plan_o1a.md)

## Selected owner declarations

A pack may add the following to `pack.toml`. Paths are relative to that pack root. An applicable selected provider may place `infrastructure.toml` beside its factory module; metadata discovery does not import the factory.

```toml
[infrastructure]
version = 1
declarations = ["infrastructure/services.toml"]
```

A declaration has `version = 1` and a `services` array. Each service has a local lowercase ASCII `id`, a `compose` path to one JSON service object, and `readiness`. The loader qualifies its identity with its owner. Dependencies use those qualified identities, including dependencies in other profiles.

Optional fields are `when = { field = "SETTING", equals = true }`, `dependencies`, `assets`, `endpoints` and `seed`. Conditions compare freshly resolved values using the composed setting type: boolean, integer and string are distinct. A missing setting declaration or incompatible comparison rejects; no executable condition is accepted.

`endpoints` entries have `name`, `host` and `in_project`. Host URLs use loopback; container URLs name the planned service. Credentials, collisions, unexpected schemes and unresolved addresses reject. Consumers receive these separate addresses after successful acquisition; the runtime does not rewrite `.env`. A Compose environment entry can use a literal string or `{ "endpoint": "owner/service/output", "address": "in_project" }`. Endpoint references must name an explicit dependency and one of its declared outputs; no ambient string interpolation is performed. Literal environment values and mount targets containing `$` are unsupported and reject before engine work.

`readiness` has `kind = "health"` or `kind = "http"`, and an integer `timeout` from 1 through 300 seconds, default 30. HTTP readiness additionally requires `target`, `identity_field` and `identity_value`, with `status` defaulting to 200. The target is a declared endpoint name plus path. Readiness checks require the expected top-level JSON string identity and reject redirects. Health readiness uses the selected engine's health status and has no HTTP fields.

A seed has `asset` and `completion`; the asset must also appear in `assets`, and completion follows the readiness format. Initialization is image-native using an admitted captured read-only mount. The runtime never runs an arbitrary seed command. Completion is checked on fresh startup and compatible reuse; reused data is not initialized again.

## Capture and admission limits

- At most 16 declarations and 32 services per operation; 64 KiB per declaration or JSON fragment.
- At most 16 assets per service, 1 MiB each and 8 MiB aggregate captured bytes.
- Service and configuration identifiers are bounded ASCII names; relative paths exclude traversal, symlinks and non-regular files.
- Unknown versions/fields, duplicate JSON keys, nonfinite JSON numbers and changes during capture reject.
- Compose admission is a bounded allowlist of prebuilt images, explicit dependencies, controlled ports, owned named volumes and captured read-only assets. Builds, privileged containers, engine sockets, arbitrary host mounts and ambient configuration are unsupported. An optional image-local command is limited to a Python interpreter running a captured read-only Python asset; it is not a host command or shell hook.

## Public runtime boundary

`load_infrastructure_contributions(context, selected_provider_locations=...)` returns immutable captured service records. The caller supplies only providers applicable to the selected operation. Pure planning validates activation, dependencies, Compose fragments and endpoint resolution before engine work. Active conditional plans require an explicit Unix engine transport; conflicting or non-Unix transports reject. Disabled plans require no engine selection. The selected Docker-compatible executable and controlled engine environment carry through validation, startup, inventory, logs and cleanup. Conditional services start in dependency order, with each readiness and seed-completion gate satisfied before a dependent starts. `plan_infrastructure(config, schema, contributions, scope, executable=..., engine_environment=...)` returns the immutable Compose plan. `PrerequisiteManager(plan, scope, ownership_directory, ...)` remains the lifecycle owner; `InfrastructureSession(manager)` adds shared timeout/progress scopes for `acquire`, `release`, `status` and `logs`. Read `session.endpoints` only after acquisition. The caller retains the manager and retries unresolved cleanup before dropping its ownership; a failed cleanup is not disposal. Conditional cleanup duties survive concurrent release and stale owners. A cleanup-only record is not an active lease. Only matching engine evidence and exact compatible container identities permit adoption or confirmed-disposal retirement; explicit retention and preexisting reused services retain their existing policy.

```python
contributions = load_infrastructure_contributions(
    context, selected_provider_locations=applicable_locations)
plan = plan_infrastructure(config, schema, contributions, scope,
    executable=engine_cli, engine_environment=engine_environment)
validate_compose(plan)  # Config-only frontend check; no services start.
manager = PrerequisiteManager(plan, scope, ownership_directory,
    engine_environment=engine_environment)
session = InfrastructureSession(manager)
session.acquire()
endpoints = session.endpoints
# Use the selected services; then release and resolve any pending cleanup.
session.release()
```

The caller runs `validate_compose(plan)` before acquisition to check the selected Compose frontend against captured input; it never substitutes another engine. This foundation is explicitly consumed by an operation; declaration discovery alone starts nothing. Run local and concrete application providers remain separate integration tasks.

- [ ] Verify these declarations, limits and APIs against source and relocated installed-pack conformance.
- [ ] Close independent review and exact applicable coverage before delivery.
- [ ] Human acceptance remains in the parent contract.
