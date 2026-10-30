# Selected-tool executable requirements

[Executable inventory](implementation_plan_p3.md)

## Contract

- [x] A version-1 tool implementation may name `executables = "executables.toml"`.
  That owner-relative, inert manifest has `version = 1`, optional `requires`
  (relative links to further manifests), and a `[tools]` mapping of fully
  qualified mise backend identities to exact three-part versions. Missing
  declarations preserve current behavior. Existing Python manifests stay unchanged.
- [x] Capture only the selected implementation's recursive manifest closure
  through existing resource ownership/receipt checks. Bound recursion, files,
  tools and bytes; reject cycles, escaping paths, executable TOML features,
  moving versions and incompatible versions within one tool environment.
  Help, collection and inactive choices never acquire executable tools.
- [x] At runtime startup, before tool factories load, prepare selected toolchains
  through mise with bounded progress, cancellation and drained subprocess cleanup.
  Materialize only validated tool data in a private temporary config; never
  activate contributed tasks/hooks/env/templates or unrelated configuration.
- [x] Give each factory an immutable `services['tool_environment']` containing
  mise's environment overrides for its own closure. Adapters merge these into
  their child environment explicitly. Do not mutate process-global PATH or
  replace application settings. Different subprocess tools may select different
  versions; one tool's contradictory recursive requirements reject.
- [x] Preserve toolchain-specific environment (Rust toolchain/home and .NET
  root/SDK selection), with managed isolated storage. Native host requirements,
  package restoration/build steps and credentials remain explicit adapter duties.
  Exact version requests do not claim owner-supplied artifact checksum locks.
- [x] Prove selected/inactive and nested-pack behavior, snapshot transport and
  narrowing, conflict/cycle/path rejection, environment separation, acquisition
  failure, timeout and cancellation. Exercise real mise acquisition and actual
  executable/version behavior separately from controlled tests. Document the
  implemented platform without inferring support from downloaded metadata.
- [x] Publish a concise README link and a practical polyglot authoring guide.
  Keep screenshots as labeled placeholders, not fabricated product evidence.

## Acceptance

- [x] Complete the applicable full unit line/branch gate.
- [x] Independent PR-style review; resolve reported ownership/classification gaps
  with causal regression tests and obtain the follow-up review.
- [ ] Human acceptance of a selected resource-pack tool's executable preparation.
  No live inference is needed to acquire toolchains.

Linux/x64 acquisition through real mise installed and ran .NET 8.0.425 and
Rust 1.90.0. Controlled tests cover source-pack relocation, captured-plan
transport/narrowing, immutable per-tool environments, recursive conflicts,
same-owner path admission and installer interruption. This does not qualify
an application-specific native adapter or another platform.

The full unit run passed 21,122 tests plus 27 subtests; a subsequent 602-case
provider/retry recheck passed against the final network classifier. Four direct
source-entry cases separately measured its script/caller branches. Joined
coverage is exact 100%: 52,696 statements and 20,732 branches, none missing.
The independent review's owner-relative path and TLS-classification findings
were repaired, regression-tested and closed in follow-up review.
