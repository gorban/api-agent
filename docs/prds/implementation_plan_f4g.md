# Session reuse and attended credential follow-up

[Bedrock requirements](implementation_plan_f4.md) · [Native login](implementation_plan_f4a2.md) · [Key qualification](implementation_plan_f4f.md)

User feedback on the bounded provider identifies the following follow-up requirements. Earlier selected-key Responses passes remain valid for their recorded scope; they do not qualify these changes.

- [x] Persist successful attended native login for the configured AWS profile across provider/process disposal and reuse valid credentials on later launches. This extends the earlier explicitly ephemeral milestone. The user requires both standard refreshable AWS profile/login or SSO cache persistence and an exported key/secret/token snapshot in the configured shared-credentials profile. Detect unchanged provider-owned snapshots and avoid letting them mask native refresh; external tuple edits must not be silently replaced. Merge only the selected profile/session/cache entries, preserve unrelated profiles and settings, use private permissions and atomic writes with concurrent-change handling, verify identity before publication, and never persist failed/unverified acquisition. Keep blocking file/helper work bounded and retain cleanup ownership.
- [x] Diagnose and fix the reported Chat Completions response rejection for openai.gpt-oss-20b in session/access-key modes. Establish the rejected public SDK/protocol shape with independent public or synthetic evidence; do not loosen unrelated validation or infer a different selected model. Preserve explicit model/API selection, terminal outcome, tool and token-usage validation, privacy and failure accounting. Cover normal maintained-SDK serialization and malformed neighboring cases with focused regression tests and causal controls.
- [x] Permit attended access_key and api_key with no unattended index when interaction is allowed: collect access-key ID then masked secret, or one masked opaque Mantle bearer key, using the existing generic private authentication screen. No vault item/index is fabricated or silently substituted. An explicitly selected index still reads only its selected password-manager item; missing indexed credentials do not change identity. NO_STD_IN, absent/canceled interaction and cancellation/cleanup must fail explicitly without secret diagnostics or hidden stdin. Do not apply AWS authorization-code normalization to these fields.
- [x] Make BEDROCK_LOGIN_METHOD discoverable with its direct/sso enumeration and help, including when omitted from saved configuration. Preserve the established cross-mode Review configuration browser; normal execution applicability remains session-only, and only the explicitly selected bundled native adapter consumes it. Explain optional/default behavior and avoid presenting unrelated adapter settings as required. Schema/help inspection stays inert.
- [x] Clarify separate remaining indexed session automation: direct AWS and federated adapters remain planned behind the same acquisition interface. Okta is one replaceable provider, not the only future indexed-session route; preserve requested TOTP behavior and same-identity recovery.
- [x] Complete focused red-to-green/causal checks with exact line/branch coverage for new runtime code, distinct implementer/reviewer checks, boundary/resource qualification and final external review before human acceptance. Full-main acceptance remains separate. Obtain new explicit spending approval before any additional live inference; the earlier until-pass key-mode authorization is fulfilled.

## Implemented scope and remaining qualification

The selected-profile persistence implementation adds worker-owned profile/cache
merge and exported shared-credentials publication after STS identity verification.
It preserves unrelated entries, uses private permissions, cooperative locking,
concurrent-change checks and per-file atomic writes with rollback. Publication is
not an atomic transaction across the filesystem. Native setup masks only its own
unchanged exported snapshot so refreshable login/SSO state remains authoritative;
user-edited credential tuples are preserved without silent replacement or source
fallback. This original behavior is superseded by [snapshot-only reuse](implementation_plan_f4j.md): both adapters verify and use valid owned snapshots before native renewal is needed. External SDK consumers can still encounter static-snapshot expiry. Constructor-frozen environment paths bind acquisition and publication to the same
locations. Persistence passes 334 focused checks at exact 100% line/branch coverage
(687 statements, 246 branches), 13 causal controls and distinct independent review.
Real profile mutation/reuse remains a separate human/live acceptance gate. The earlier ephemeral milestone remains
historical evidence, superseded for persistence by these requirements. See
[session behavior](../../src/api_agent/llm_providers/bedrock/SESSION_LOGIN.md).

Attended keys pass 187 focused offline tests with exact 100% line/branch coverage
for auth, native interaction and the opaque-key interaction modules; five causal
controls reject value normalization, forbidden prompting, canceled-owner
admission, selected-identity substitution and lost cleanup ownership. Distinct
implementation review is complete. Configuration discoverability passes 11
focused checks and distinct review: omitted direct/sso settings remain browsable
in cross-mode Review configuration while execution stays session-only/native-only.
Indexed direct AWS and federated acquisition, including replaceable Okta adapters
and requested TOTP/same-identity recovery, remain planned implementation work;
checking the clarification above does not complete that automation.

The initial empty-annotation compatibility correction retained its independently
reviewed offline scope. A bounded authorized live structural probe then confirmed
that Mantle returns the selected model with a nonstream `reasoning` string. The
codec now validates this alias and `reasoning_content` independently and suppresses
both from visible results. Malformed types, encoding and size remain rejected;
streaming behavior is unchanged. Distinct review passes 292 focused checks with
exact 100% codec line/branch coverage (360 statements, 100 branches). The existing
integration test then passed through the real provider in Completions/access_key
mode with the selected OSS 20B model and a 256-output-token bound: 130 total tokens.
Only this final request has usable accounting here; earlier failed inference usage
remains unavailable rather than zero. Session-mode Completions and live persisted
login reuse remain human/live gates. Earlier Responses checks retain their scope.

The final combined provider checks pass 1,230 focused tests with exact 100%
line/branch coverage across all 16 executable provider modules (2,241 statements,
762 branches). Distinct lane reviews and causal controls are complete.
External review found and the implementation closed an SSO section separator defect
for valid profiles ending without a newline. Seed/publication/restart regressions,
a causal control and distinct review pass; the repair passed external re-review.
Repeated exact public-boundary and wheel/sdist checks passed for this repair.
Installed and relocated probes cover ordinary parsed-flow acquisition, synthetic
attended keys and a synthetic persisted-profile restart. Final external review approved the repaired candidate. Human acceptance remains
open. Real persisted-login reuse across processes and
SSO interoperability are separate live gates. No additional installation change
is required beyond the existing AWS CLI v2 native prerequisite. Existing spending
approvals apply only to their specific bounded tests; this document authorizes
no additional live authentication or inference.

## Deferred human acceptance

- [ ] Through ordinary launch.sh, complete attended native login, verify selected-profile refresh cache and credentials snapshot persistence, then restart and confirm reuse.
- [ ] Exercise no-index attended access_key/api_key prompts and restored direct/sso method setting through the public launcher.
- [ ] Qualify session-mode Completions and live SSO interoperability separately; request explicit inference authorization for further coordinator-owned live requests.
