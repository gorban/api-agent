# Declared environment controls

[Configuration and TUI](implementation_plan_a.md)


- Application behavior controlled by environment variables must have typed TOML
  declarations and follow normal dotenv/environment/CLI precedence. Keep bootstrap
  selectors, standard OS/SDK inputs, and internal launcher/worker protocols
  explicitly distinguished from user application settings.
- Non-sensitive test fixture inputs may opt into resolved unit execution worker
  environment transport with `unit_environment = true`; the default is false.
  Terminal and TUI execution must pass the same resolved values, preserving absent/inactive
  fields as unset and explicit empty values as empty. Only collection-neutral
  fixture inputs may opt in; this execution-only contract does not control
  collection or alter its environment.
  Sensitive declarations must reject this opt-in.
- Workflow renderer artifact selection must use its declared path setting for
  static regeneration and temporary selected-flow rendering, including explicit
  CLI overrides, without requiring application/provider startup.

A declaration alone does not change a consumer's value sources. Consumers that
still read ambient environment need a resolved handoff before claiming normal
dotenv/environment/CLI precedence. A declaration inventory does not close that
separate migration requirement.


## Acceptance

- [x] Confirm full unit/coverage and public boundary gates on the final source.
- [ ] Human review of declared artifact selection and retained unit evidence remains open.
