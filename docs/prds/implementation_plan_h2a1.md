# Shared reporting identity and publication-only access

[Runtime ownership](implementation_plan_m.md) · [Infrastructure](implementation_plan_h.md).

## Current routing decision

The endpoint URL alone selects each reporting backend. The implemented UUID/trusted-registration contract below is historical, not a prerequisite for tracing or publication. User review superseded that design; it did not accept it for runtime wiring. Local container ownership remains separate, internal lifecycle evidence. See [current lifecycle requirements](implementation_plan_h2a2.md) and [tracing attribution](implementation_plan_k1.md).

- [ ] Retire unused installation-ID/registration selection contracts and their obsolete tests when wiring endpoint-only routing. Preserve independently useful URL validation and publication-versus-administration separation; test that remote publication needs no local ownership record. Renew applicable gates for the resulting code changes.

## Historical implemented scope

Establish pure, independently testable identity and access contracts before persistent service wiring. Existing prerequisite, Compose, launcher, listener and storage behavior remains outside this slice. The contract performs no discovery, authentication, container administration, filesystem migration or network calls.

## Requirements

- [x] Explicit selection distinguishes a managed shared reporting installation from an external reporting endpoint. Endpoint health alone never creates an ownership claim.
- [x] Managed installation identity is explicit and independent of checkout paths. Publication binding validates the selected installation and endpoint against a trusted owner record. Identity matching is not an administrator credential or a grant of management authority.
- [x] External selection grants publication access only and does not infer a managed owner, installation or cleanup right from a URL, port or health observation.
- [x] Define conservative endpoint equivalence with safe input validation. Reject embedded credentials and unsupported/ambiguous endpoint forms; diagnostics exclude raw unsafe input. Canonical values and identities contain no credentials or checkout-derived fields. Determine literal categories before normalization: brackets admit only supported IPv6; empty hexadecimal numeric components are rejected rather than treated as DNS.
- [x] Publication access permits publishing and relinquishing access; it cannot stop/recreate the service or delete data, even for the last consumer. Publication release decisions preserve service and data. A separately authorized lifecycle owner may stop managed containers after the last TUI/session and active operation release; normal shutdown still retains data. Administrative authority, durable cross-process leases, consumer liveness, migration and runtime execution require the separate [lifecycle and management implementation](implementation_plan_h2a2.md) and review.
- [x] Validate malformed declarations and mismatched owner/endpoint claims without fallback. Use synthetic fixtures and isolated mutations that prove identity/access guards independently.
- [x] Complete independent adversarial review, applicable exact unit line/branch coverage, wheel and public-boundary checks.
- [x] Human routing decision: use backend URL alone, with no installation-ID or registration requirement. Earlier matching/mismatch acceptance is superseded, not an approval to wire that design.

No new configuration value or setting precedence is introduced here. The interface will be connected to normal resolved configuration in the runtime-wiring slice; existing suite exclusion and authoritative history remain intact.

## Historical contract and review examples

`runtime.reporting_identity` exposes immutable `InstallationId`, `ReportingEndpoint`, explicit `ManagedSelection`/`ExternalSelection`, and `PublicationAccess`. Managed access requires `ReportingRegistration` from a separately trusted registry and checks both fields through either public entry point. Registration is routing evidence, not authentication. `release()` is a pure retention disposition; it does not implement a live lease or revoke a connection.

The earlier review covered these scenarios; they are not current runtime prerequisites:

1. Matching UUID and normalized endpoint accept publication. A wrong UUID or endpoint rejects it even when the other field matches.
2. Scheme/host case, default ports and an empty root normalize. Non-root path case, trailing slash, scheme and nondefault ports remain distinct. Embedded credentials, query/fragment, escapes, dot segments and ambiguous numeric hosts fail with bounded diagnostics.
3. External publication has no inferred managed identity. Both access modes allow publication/relinquishment, reject stop/recreate/delete, and retain services/data on release.

The synthetic acceptance controls are in `tests/unit/test_reporting_identity.py`. No endpoint is contacted. Registration matching is now superseded. Local lifecycle leases, migrations and endpoint-only configuration integration remain follow-up work.

## Verification

Independent review repaired numeric-host validation and strengthened direct-initialization rejection controls. All 102 contract cases pass; nine isolated guard mutations are detected. The intended-change snapshot passes 5,952 unit tests plus 27 subtests with exact 100% line/branch coverage (134 files, 17,683 statements, 6,486 branches). Wheel contents/license and intended-index/public-metadata checks pass. Broader in-flight changes were excluded; the later endpoint-only decision supersedes the original acceptance scenario.

Endpoint validation follow-up: reject unsupported bracketed literals and empty hexadecimal numeric components. All 121 focused controls pass; ten isolated guard mutations and adjacent input checks validate the repairs. The isolated full suite passes 5,971 units plus 27 subtests at eight workers with exact 100% line/branch coverage. Wheel and public-boundary checks pass. Endpoint-only runtime wiring remains follow-up work.
