# M3A — provider distributions and declaration authority

## Required behavior

- Provider Python requirements are declared in a versioned `provider-dependencies.toml` beside the provider distribution. A selected provider is represented by an inert manifest descriptor with an owning root and identity. Only providers active for the current operation contribute requirements. Help, configuration discovery, unit collection, and unrelated operations do not prepare their dependencies.
- Base and resource-pack project requirements remain in their existing `pyproject.toml` manifests. Provider requirements join the same target-interpreter verification and environment preparation step. Every selected requirement is parsed as PEP 508 before any manifest-dependent installation. If the host has neither parser, the launcher first reuses the target interpreter's parser or prepares a seeded environment/fixed validator; no manifest value enters that bootstrap installer. Direct URLs, malformed requirements and installer options are rejected. A failed target-interpreter probe halts preparation; valid missing packages alone are installation intent. A fresh host needs neither `packaging` nor pip installed globally. The target environment supplies the standards parser; inability to prepare or verify it yields a launcher error. Native executable preparation is outside this Python requirement contract.
- A relocated installed resource pack must carry `pack.toml`, its configuration declaration, provider configuration declarations, production source directories and dependency manifests beside selected provider factories. These files are checked without importing a factory. Manifest reads reject nonregular files and compare file identity after opening; systems without nonblocking/no-follow open flags retain an unverified native replacement-race guarantee. Source-tree factories remain supported.
- A shared field owns its type and safety constraints. A pack or provider may add a new field. Replacing an existing field requires an explicit override; omitted constraints inherit. Explicit overrides may change defaults, conditional defaults, help, anchor, applicability and review presentation. Existing requiredness, validators, enum restrictions, `pack_only`, and `cost_affecting` cannot be weakened. An existing required predicate must remain unchanged; changing its logic needs a separately specified proof of stronger semantics. A later pack default still takes precedence when its value is valid.

## Acceptance and remaining join

Relocated-pack inspection uses the same inert provider discovery as launch,
including each selected factory's own declarations, contributed selectors and
cycle checks. Valid empty override defaults are checked after omitted safety
constraints inherit, for both static and conditional defaults. Standalone
malformed defaults and explicit constraint weakening remain invalid.
Late-target overrides defer effective value checks until selected-provider
discovery supplies their target constraints. Known and deferred incompatible
types must reject before validators inherit, with a controlled launcher error.
Compose all sibling fields and their inherited constraints before validating
effective override predicates. Controller declaration order in a single file
must not affect applicability, requiredness, conditional defaults or inert help;
typed controller validation and override authority remain enforced.
Preserve each accepted contribution's original source position across immediate
and deferred phases. Replay raw declarations in that order after discovery;
retain effective own constraints and validate every captured declaration against
final controller constraints. A valid later default/constraint tightening wins,
and an invalid earlier contribution cannot disappear behind it. Only an actual
selected-provider introduction can supply an earlier optional override target;
an ordinary later new field remains at its own source position. Keep canonical
selector paths tied to their contributing owner and preserve final graph guards.
Capture each selector's directory at discovery. Later provider declarations may
change defaults and their owning directory, but cannot change the discovery
scope of explicit .env, environment, CLI or interactive inputs. Canonical final
comparisons use that captured scope and still reject different/invalid targets.
Pending controllers are lookup-only context, never live fields or authority.
Unknown controllers first declared in a later file remain unsupported unless
already present in earlier pending context. Missing actual targets invalidate
dependent predicates. Validate all fields returned by direct composition,
including newly added fields, against actual fields or explicit pending context;
never return a dangling predicate. Validate inherited defaults/types, empty
integer/path values and nested predicates without weakening constraints.
Empty integer controllers resolve to `None` and match an empty predicate value;
an omitted controller remains distinct. Scalar, list and nested predicates use
the same comparison in configuration, review and accounting consumers.

- [x] Pure manifest reader, active-kind selector and additive selected-provider dependency API, with synthetic active/inactive provider checks. The bundled GPT provider declares its HTTP/auth dependencies and an optional browser group. Per-capability browser-group selection remains a future refinement.
- [x] Relocated synthetic whole-pack check covers manifest, primary schema, provider schema, defaults, selected factory files and production tree without executing a factory.
- [x] Explicit field override authority and focused weakening checks preserve normal pack default precedence. Selected provider declarations must state `override = true` for existing fields; bundled GPT does so for its shared credential role.
- [x] Accepted integration startup passes selected active provider manifest descriptors to the environment gate; consumer controls verify inactive selections and inert help/collection. The future local-operation seam uses the same activation matrix.
- [x] Build a wheel and verify the extracted, relocated package resolves its bundled demo pack, primary schema, selected factory and selected provider manifest without importing the factory. The archive carries the declarations and manifest.
- [x] Independent adversarial review, including direct-URL/options rejection, explicit authority, valid validator strengthening and same-source rebootstrap. Different declaration sources require an explicit override even when their values match.
- [x] Human acceptance of relocated declarations, configuration review and selected dependency preparation, recorded October 12, 2026.

Earlier follow-up repairs passed independent adversarial review, seven isolated mutation
controls and 5,572 base unit tests with exact per-file 100% line/branch coverage.
The wheel builds and its extracted relocated pack includes the selected
factory's declarations without importing it. Shared execution finalization,
bounded native-cache reads and reset-overlap controls also pass. This offline
verification does not complete deferred live native-account/meter acceptance.

Source-order/direct-API repair verification:

- [x] Preserve all immediate/deferred file pairings, valid later tightening,
  contribution identity and lookup-only pending controllers. Only selected
  provider introductions supply provisional targets; ordinary introductions
  retain their source position. Equivalent canonical selector targets are
  accepted in their owning scope; different/invalid targets remain rejected.
- [x] Direct composition, contribution and provider-schema APIs reject missing
  controllers on new fields across all three predicate forms, with successful
  actual-controller controls. Earlier invalid declarations remain detectable.
- [x] Fifty new controls and 320 independent focused tests pass. Isolated
  mutations detect missing direct validation, ordinary-field promotion,
  raw selector comparison and wrong explicit-selector ownership.
- [x] Renewed full public gate: 5,797 unit tests plus 27 subtests; exact 100%
  lines/branches across 132 measured files (17,517 statements, 6,442 branches).
  The wheel and extracted inert declaration checks pass. The 391-file
  intended index and commit metadata pass the content-bound public audit.
  No live inference is needed for these declaration checks.

Captured-selector scope verification:

- [x] Capture each selected target's discovery directory before later defaults
  change ownership. Four explicit-input sources retain this scope, including
  relative selectors, existing/missing shadow files and real inert help.
- [x] Fourteen additional controls pass; independent review passes 237 focused
  tests. Isolated mutations detect restoring mutable ownership and dropping the
  canonical target check. Equivalent defaults retain their own source directory;
  genuinely changed targets remain rejected.
- [x] Renewed full gate: 5,811 units plus 27 subtests, exact 100% lines/branches
  across 132 measured files (17,520 statements, 6,442 branches). The wheel and
  extracted inert tests for all explicit sources, both shadow cases and actual
  help pass. Whitespace, the 391-file indexed boundary audit and commit metadata
  checks pass. No live authentication or inference is required.
