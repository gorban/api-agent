# Resource-pack bootstrap, configuration and worked example

[Integration-guide requirements](implementation_plan_c1.md)

- [ ] Show `api-agent/` and an independently named application pack as sibling
  directories. Label almost every extension path optional; identify the real
  minimum manifest/configuration requirements and whether `launch.py` is only
  needed for launching from the pack. Include the pack's own README, LICENSE and
  notices, `pack.toml`, `cfg/config.toml`, optional provider-local configuration,
  password-manager registration, workflow root/nested declarations, prompts,
  schemas, tools, provider implementations, dependencies and tests. Use synthetic
  names and public contracts only, not copied application material.
- [ ] Supply a runnable wrapper `launch.py` example defaulting to sibling
  `../api-agent`, plus `API_AGENT_DIR` and `--api-agent-dir` overrides. Explain how
  it finds the shared source entry before importing it; simply importing a package
  not yet installed is not a fresh-bootstrap solution. Preserve interpreter,
  argument forwarding, selected pack root and exit status; work from a different
  CWD or renamed checkout. Show direct `--resource-pack-dir` and
  `RESOURCE_PACK_DIR` alternatives. Distinguish source checkout launch from wheel
  consumption rather than implying an installed wheel includes a source launcher.
- [ ] Explain that application bootstrap is pack selection plus typed declarations
  and defaults, not a requirement to fork core or an invented `BOOTSTRAP` setting.
  Include manifest identity/owner/config paths, ordered `providers.configs`, suite
  ownership, optional production and identity declarations, and the selected
  workflow entry using the format actually delivered at documentation time.
- [ ] After the tree, map each optional directory/example to its real selector or
  declaration: `LLM_PROVIDER`, `UI_PROVIDER`, `API_PROVIDER`, provider-scoped
  `UPSTREAM_API_PROVIDER`, `ACCESS_PROVIDER`, password-manager registration and
  `UNATTENDED_AUTH`, role/index declarations, workflow/resource bindings and tests.
  Show both a single independently selected provider and a complete pack whose
  source-controlled defaults select all its providers without `.env`/environment
  assignments. Link authoritative config declarations instead of duplicating the
  full settings inventory. Explain fields needed only by consuming operations.
- [ ] Document schema/default composition separately from explicit value precedence.
  Cover shared declarations, pack additions/defaults, ordered contributions and
  selected provider declarations, inheritance and compatible constraint changes.
  Explain provider contributions overriding pack defaults while later declared
  sources retain their intended precedence, owner-relative selectors, deferred
  sibling composition, conditional applicability/requirements/defaults and cycle
  rejection. Providers must not override explicit user configuration.
- [ ] Show the actual explicit-value order: base dotenv, configured shared dotenv
  files in declaration order, selected-pack dotenv, environment and CLI, with the
  actual behavior for absent versus explicit empty values. Verify this against
  the resolver before publication. Explain declaration-owner path anchors versus
  CWD and how bootstrap checkout selection precedes ordinary configuration.
- [ ] Add a small precedence example and an example new required typed field with
  an operation condition. Explain TUI default acknowledgement/provenance and why
  reviewing configuration/help/collection does not import factories or initiate
  credentials, containers or paid inference.
- [ ] Explain that separate external packs/providers retain their own licensing and
  copyright notices; keep api-agent and third-party notices intact when adapting
  or redistributing their actual material. Recommend upstream checkout updates
  and conformance checks rather than application edits inside shared source.
- [ ] Recommend optionally adapting the public `AGENTS.md` into the application's
  own guidance, preserving applicable notices. Explain its actual benefits:
  readable pytest behavior tests, focused mutation controls, exact coverage gates,
  shared nested timeout/progress and original managed-browser CDP inspection with
  Chrome DevTools MCP preferred and Playwright MCP supported. Do not promise
  measured token savings or describe the future bounded workflow inspector as
  available. Link that diagnostic guidance once implemented. Copying the file is
  not required to consume the runtime and does not change either repository's
  approval rules; this task does not edit api-agent's `AGENTS.md`.
- [ ] Validate the minimal and full skeletons, wrapper defaults/overrides and
  declaration examples in temporary independently authored packs. Check help and
  collection without login, source/relocated package imports, public interface
  conformance, optional omissions and broken paths. Reuse existing offline checks;
  do not require live credentials or paid inference merely to certify prose.
- [ ] Before final README/guide acceptance, reconcile Run local, managed API hosting,
  UI login and the final `flow.md.html` grammar/links with their owning delivered
  requirements. Walk through the guide from a fresh developer's perspective;
  retain unverified platform behavior explicitly instead of declaring it proven.
