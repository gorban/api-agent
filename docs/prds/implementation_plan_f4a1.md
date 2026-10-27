# Replaceable Bedrock session acquisition

[Authentication requirements](implementation_plan_f4a.md)

This phase introduces a provider-local acquisition interface and temporary-credential lifecycle. Direct AWS browser login, IAM Identity Center, federated helper automation and live account qualification remain separate adapter phases. The builtin profile path continues to require preauthenticated credentials and rejects indexed accounts.

## Selection and adapter contract

- [x] Contribute BEDROCK_SESSION_PROVIDER as an optional file.py:factory selector. Empty selects the builtin SDK/profile adapter. Resolve relative paths against the owner root. Import and construct the adapter only during explicit session setup; provider construction, schema/help/configuration inspection and collection remain inert. No fallback after failure of an explicitly selected adapter.
- [x] Use a synchronous `(config, services)` factory returning a version-1 adapter with asynchronous `acquire(request, *, previous=None)` and `aclose()`. Validate integer version, coroutine hooks and signatures. Support package-relative imports and retain imported modules until disposal; safely close malformed owners and remove owned modules after successful cleanup. Cleanup failure remains retryable.
- [x] Pass an opaque immutable SessionRequest containing profile, region, the unchanged selected user index, effective interaction permission, selected credential reader and optional interaction owner. NO_STD_IN disables interaction even if a caller requests it. Scope reader calls to the selected index. Missing input must never trigger hidden stdin or another account; concrete TUI challenge handling remains a later adapter integration.

## Material and lifecycle

- [x] Return opaque immutable SessionMaterial for legacy custom profile material, frozen SDK credentials, or complete temporary credentials. Builtin acquisition resolves and STS-checks one frozen tuple before use. Static frozen keys need no token/expiry; unknown-expiry token sources use a separate bounded re-probe deadline. Temporary material requires nonempty access key, secret, session token and finite future expiry. Reject malformed tuples and selected-index mismatches. Profile material cannot claim an indexed identity or carry explicit keys. Future indexed adapters must verify the effective AWS identity before declaring bound material; matching an index field alone is not live identity proof.
- [x] Keep credentials in a provider-owned callable/disposable session. Coalesce concurrent acquisition and refresh; refresh approaching expiry and reject expired material. A cancelled waiter must not cancel another waiter's acquisition. Own any continuing acquisition until cleanup. Failures never restore another source, stale material or ambient credentials.
- [x] Reject use after close, prevent late material publication, drain pending acquisition and dispose the adapter. Cleanup cancellation/failure preserves an actionable retry owner; no orphaned login/refresh operation. Use shared deadlines and cancellation ownership throughout. Fixed safe diagnostics and opaque reprs must not expose adapter exceptions, key fields, readers or interaction transcripts.
- [x] Dispose a successfully acquired session if cancellation wins before delivery to its caller. Revoke handed-off credential readers at ownership end, including failed setup; reject new reads, drain in-flight helper cleanup without repeated cancellation from explicit paths or deadlines, and preserve every retained owner when independent cleanup operations fail.

## SDK and qualification

- [x] Before each SDK request, refresh through the auth callable and capture one worker configuration snapshot. Use that same snapshot for payload and environment policy. Pass complete temporary key/secret/token explicitly to the isolated SDK worker; do not pass a profile or allow ambient credentials to replace that tuple. Keep credential-bearing data confined to owned memory and worker stdin; no argv or ordinary diagnostic copies.
- [x] Preserve existing worker process-tree cleanup, error classification and stream validation. No inference/authentication is triggered by the new configuration field itself.
- [x] Verify actual configuration/provider/runtime joins, explicit adapter load failures, relative imports, expiry/refresh races, index isolation, cancellation, cleanup retry and secret exclusion using deterministic offline tests. Require exact 100% line and branch coverage for new modules, meaningful causal mutations and focused affected regressions. Verify source/installed/relocated resources and independent adversarial review before acceptance.

Live authentication requires account-readiness confirmation. Any inference-spending test requires separate explicit authorization and bounds. Successful offline acquisition or temporary-token transport does not qualify a concrete AWS/SSO login adapter, pricing or production adoption.
