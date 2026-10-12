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
Retain deferred siblings as one contribution-file batch and replay batches in
source order after discovery. Validate each batch against its effective sibling
controllers; preserve contribution ownership and precedence between files.
An invalid earlier contribution must not disappear behind a later override.
This does not permit unknown controllers declared only in a later file.

- [x] Pure manifest reader, active-kind selector and additive selected-provider dependency API, with synthetic active/inactive provider checks. The bundled GPT provider declares its HTTP/auth dependencies and an optional browser group. Per-capability browser-group selection remains a future refinement.
- [x] Relocated synthetic whole-pack check covers manifest, primary schema, provider schema, defaults, selected factory files and production tree without executing a factory.
- [x] Explicit field override authority and focused weakening checks preserve normal pack default precedence. Selected provider declarations must state `override = true` for existing fields; bundled GPT does so for its shared credential role.
- [x] Accepted integration startup passes selected active provider manifest descriptors to the environment gate; consumer controls verify inactive selections and inert help/collection. The future local-operation seam uses the same activation matrix.
- [x] Build a wheel and verify the extracted, relocated package resolves its bundled demo pack, primary schema, selected factory and selected provider manifest without importing the factory. The archive carries the declarations and manifest.
- [x] Independent adversarial review, including direct-URL/options rejection, explicit authority, valid validator strengthening and same-source rebootstrap. Different declaration sources require an explicit override even when their values match.
- [x] Human acceptance of relocated declarations, configuration review and selected dependency preparation, recorded October 12, 2026.

Follow-up repairs passed independent adversarial review, seven isolated mutation
controls and 5,572 base unit tests with exact per-file 100% line/branch coverage.
The wheel builds and its extracted relocated pack includes the selected
factory's declarations without importing it. Shared execution finalization,
bounded native-cache reads and reset-overlap controls also pass. This offline
verification does not complete deferred live native-account/meter acceptance.
