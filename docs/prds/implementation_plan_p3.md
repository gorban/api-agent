# Executable inventory and discovery boundaries

[Mise startup](implementation_plan_p.md) · [Conditional preparation](implementation_plan_p2.md)

## Base inventory

- [ ] Keep one root `mise.toml` and checked `mise.lock` with explicit tool versions.
  Request only Python/uv at entry. Node/npm, Java/Allure and mkcert are optional
  and prepared at their established operation boundaries.
- [ ] Preserve backend identities, platform URLs/checksums and the mise minimum
  version. Launch consumes pins; it does not silently refresh them.
- [ ] Keep Python packages with the existing Python resolver and Node packages
  with npm/package-lock.json. Mise supplies executables, not every library.
- [ ] Select the source checkout's mise configuration without activating unrelated
  parent/global configuration or project env/hooks/tasks. Keep application .env
  precedence and caller-relative paths in the existing Python bootstrap.
- [ ] Support ZIP startup without Git. Git-dependent features retain their own
  capability requirement; automatic Git acquisition is not promised.
- [ ] Keep engine access, host OpenSSL/trust and account permissions outside the
  downloadable executable inventory unless separately qualified.

The actual versioned inventory is [mise.toml](../../mise.toml), with its
[artifact lock](../../mise.lock). Do not duplicate it as a second authoritative
configuration in documentation.

## Existing recursive discovery

The current [provider dependency declarations](../../src/api_agent/providers/distribution.py)
and [selected tool service capture](../../src/api_agent/tooling/services.py) remain
the authority for selected requirements. Their existing Python dependency and
container/service contributions continue unchanged.

The [selected-tool executable extension](implementation_plan_p3a.md) extends those
same paths rather than recursively loading arbitrary mise files or introducing
another workflow catalog. It is additional to the surgical launcher migration.

- [x] Define the additive selected-tool manifest and exact-version identity
  contract, preserving version-1 Python declarations. Pack-owned artifact-lock
  passthrough is explicitly not part of this extension.
- [x] Follow only admitted selected tool executable dependencies, including
  nested contributions, with cycle detection, compatible deduplication and clear
  conflict reporting. Unselected discovery must not install, authenticate or start
  services.
- [x] Keep external component inventories with their owners; do not copy private
  sources into the base inventory. Reject unsupported same-process version conflicts
  instead of letting mise directory precedence select a winner.
- [x] Admit executable data through the existing bounded ownership/path checks.
  Do not run component env/hooks/tasks as a side effect of discovery.

## Verification and acceptance

- [ ] Exercise real Python/uv acquisition and selected optional tools, not only TOML
  parsing. Verify actual paths/versions and optional-tool omission.
- [ ] Keep generated platform lock entries distinct from executed platform support.
  Qualify supported macOS/arm64 and fresh-host capabilities before claiming them.
- [x] Verify source/pack selection and existing recursive Python/service
  discovery alongside the separately qualified selected-tool executable extension.

Public contracts: [mise exec](https://mise.jdx.dev/cli/exec.html),
[install/laziness](https://mise.jdx.dev/cli/install.html),
[locks](https://mise.jdx.dev/dev-tools/mise-lock.html).
