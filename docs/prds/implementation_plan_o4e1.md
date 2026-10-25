# Declaration-bound infrastructure inventory

[Tool ownership](implementation_plan_o4e.md)

Different application and test graphs may select different services inside one managed project. A retained service that is outside the current selection must not block another operation solely because it is unselected. Recognition of such a neighbor must remain separate from permission to acquire, reuse or dispose it.

## Identity admission

- [ ] Capture an immutable inventory catalog from the normal launch context's explicit infrastructure declarations and already admitted selected-tool owners. Bind it to the same physical base and resource-pack scope as the operation.
- [ ] Capture passive declaration identity only: bounded regular-file TOML, supported envelope/version, owner-qualified service IDs, canonical owner/root provenance and derived exact Compose service/profile pairs. Do not import factories, compose inactive configuration, evaluate activation, read inactive Compose fragments/assets or acquire prerequisites to obtain passive identities.
- [ ] Derive standard service/profile pairs from the controlled base Compose declaration. Reject cross-pairs, unknown services/profiles, conflicting captures, owner/root mismatches and generated-name collisions. Managed, scope and project labels must all match. Preserve legacy-scope rejection.
- [ ] Carry the parent's captured catalog through protected worker admission with bounded structural, derivation, provenance and scope validation. Case narrowing may retain passive scope identities but must still narrow active execution and prerequisite admissions. A self-consistent hash alone does not grant authority to worker-supplied sources.
- [ ] Keep the captured authority fixed throughout acquisition and cleanup. A removed or unavailable declaration does not authorize a retained service through labels or historical cache entries.

Version 1 pack infrastructure metadata may explicitly add passive sources through an optional `inventory` array, defaulting to empty. Each row has `owner`, `root` (`base` or `pack`) and a bounded `declarations` list of relative TOML paths. These rows authorize identity capture only; ordinary active `declarations` keep their existing meaning. This explicit list allows application-to-test and test-to-application transitions to recognize retained services without discovering inactive implementation graphs.

## Passive and active behavior

- [ ] Passive recognition permits inventory inspection and leaving an unselected neighbor untouched. It grants no lease, endpoint, readiness, compatibility, log access, storage access, acquisition or cleanup responsibility.
- [ ] Selected services retain all existing image/digest, fingerprint, storage, readiness, stopped-object lease and immutable-container-ID checks. Passive membership cannot bypass selected compatibility failures.
- [ ] Preserve reporting's exact registered-object validation. An unrelated recognized service must not be added to reporting ownership or cleanup sets to satisfy a check.

## Shared lifetime

- [ ] Use existing per-operation leases and the scope lock for shared services. Never stop a container while another live compatible lease references its immutable ID.
- [ ] When an operation requests cleanup and another compatible operation still uses the service, transfer eligible cleanup responsibility under the lock. Whichever operation releases last performs that cleanup, regardless of which operation originally created the service. Apply this to standard profiles and declared custom services, including shared backing services.
- [ ] Bind transfers to the same engine and immutable container IDs. Keep passive neighbors, unrelated services and later replacements outside cleanup responsibility. Preserve explicit retention and volume-preservation policy; last use does not mean unconditional deletion.
- [ ] Validate every transferred cleanup ID against its originating lease's bounded container-ID set before using journal data. Malformed or conflicting records must not expand cleanup authority.
- [ ] Failed final cleanup retains its owner and retryable responsibility. Cancellation of application work must not discard cleanup or interrupt the protected release protocol.

## Verification and acceptance

- [ ] Prove a declared application neighbor and a distinct selected test closure coexist without inactive configuration, asset or factory access.
- [ ] Reject wrong scope/project/managed labels, unknown or crossed pairs, forged or stale source authority, selected incompatibility and container replacement.
- [ ] Prove sharing in both release orders, different selected subsets, creator-first handoff, final cleanup retry, cancellation and explicit retention. Verify selected services stop only after their last lease; passive neighbors remain untouched.
- [ ] Retain applicable exact unit coverage, causal rejection checks, installed/relocated artifact checks, independent and external review, and normal live integration evidence.
- [ ] Human acceptance of the resulting selected-tool lifecycle remains explicit and separate from automated verification.


## Passive owner directory metadata

- [ ] Permit an optional `directory` string in each version 1 explicit inventory row. Resolve it relative to the existing `root = "base" | "pack"` anchor; omission or exactly `"."` retains the anchor as the owner root. Interpret `declarations` relative to that resulting owner directory.
- [ ] Require a bounded canonical relative directory path, with `"."` the sole special case. Reject non-string, empty, absolute, parent traversal, empty or dot components, backslash, missing, non-directory, symlinked and anchor-escaping paths. Keep bounded no-follow regular-file TOML capture for declarations.
- [ ] Preserve selected tool owner roots and declaration-relative assets. An explicit passive directory must agree with an admitted owner's canonical root; duplicate declaration paths must not bypass root-conflict rejection.
- [ ] Verify actual default, shared-fixture and default-again selections retain one exact passive identity set and canonical fixture owner root without inactive factory execution, inactive asset/configuration reads, prerequisite acquisition or changes to active owner fingerprints. Verify the actual metadata in installed and relocated package artifacts.
