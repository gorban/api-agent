# Attended native AWS sign-in adapters

[Authentication](implementation_plan_f4a.md) · [Acquisition contracts](implementation_plan_f4a1.md) · [Runtime configuration requirements](implementation_plan_a9k.md) · [Run local interaction ownership](implementation_plan_a9k1.md)

Implement an optional bundled version-1 session adapter for direct AWS console-to-SDK login and IAM Identity Center device authorization. This bounded milestone qualifies attended native CLI handoff behind the existing replaceable interface. It does not establish automated browser-form filling, indexed unattended human identity, the optional federated helper or full live AWS acceptance.

## Selection and identity

- [x] Select the bundled aws_login.py:factory explicitly through BEDROCK_SESSION_PROVIDER. Keep schema/configuration/help/provider construction inert. Resolve selected SDK credentials first in an owned bounded process; valid credentials need no native configuration or interaction. The initial BEDROCK_LOGIN_METHOD selector is superseded by [separate direct/SSO factories](implementation_plan_f4h.md); BEDROCK_AUTH_MODE remains session. Builtin selection never starts CLI recovery; custom adapters retain exclusive ownership. Native recovery is allowed only for recognized missing/expired/invalid credentials, including actual ProfileNotFound during selected-profile construction; retain that requested name for isolated recovery and never try another SDK profile or chain. Network, denied access, malformed profile, dependency and arbitrary failures stop. After ambient selection, refresh stays bound to its verified principal and never falls back to native login. Mantle failures never change sources. Native settings remain visible in session mode to support relative, absolute and relocated selectors; default acknowledgment is disabled, ordinary schema validation still applies, and only this adapter consumes them.
- [x] Allow optional independent non-secret expected 12-digit AWS account and principal assertions; require neither for valid selected credentials or direct attended sign-in. Match an exact IAM user ARN or an STS assumed-role ARN prefix without its final variable session-name segment. Verify readiness and any supplied Account/Arn assertion using STS authenticated by the exact frozen tuple before publication and on refresh. A shared role principal does not establish a particular human identity.
- [x] Reject selected unattended indexes before helper launch in this milestone. Do not read vault credentials, discard an index, infer an AWS identity from a vault name or substitute ambient profiles. Indexed direct/federated automation remains separate work with stronger identity binding.
- [x] Require nonblank profile/region for attended recovery; ordinary application defaults supply them. Ambient-first resolution supports explicit SDK blanks before recovery is needed. Preserve requested profile/region throughout the adapter lifetime and reject binding changes. Maintain complete key/secret/token/future-expiry material using the canonical SessionMaterial type, including when loaded through a dynamic factory namespace.

## Persistence extension

The checked isolation/cache requirements below record the original ephemeral
milestone. [Session reuse follow-up](implementation_plan_f4g.md) supersedes its
no-shared-update and no-persistence limits: successful verified native login must
retain the selected refreshable profile/cache entries and an exported configured
shared-credentials snapshot. Final combined integration, external and live
persistence acceptance remain open there. Unrelated profiles and external tuple
edits must be preserved; per-file atomic writes/rollback do not promise an atomic
filesystem-wide transaction.

## Isolated native handoff

- [x] Require supported AWS CLI v2, at least 2.32.0, only during acquisition. Use documented direct aws login --remote or SSO aws sso login --use-device-code --no-browser. Do not launch an unmanaged browser. Export with aws configure export-credentials --format process; require Version1, nonblank key/secret/token and timezone-aware future Expiration. Unqualified CLI versions fail with canonical prerequisite guidance preserved through both adapter/session wrappers using fixed sanitized messages.
- [x] Create only adapter-owned temporary HOME/config/credentials/login and SSO caches with private directory/file permissions. Generate SSO configuration from selected-profile routing or explicit HTTPS start URL, SSO region, separate SSO account and role name. Request only missing routing after ambient credentials are unusable, through canonical fields-only ConfigurationRequiredError. Never copy arbitrary profiles. The original milestone did not update shared profile files or persist beyond adapter lifetime; the [persistence extension](implementation_plan_f4g.md) now requires verified selected-entry publication. Ordinary SDK refresh/helpers retain their documented behavior; temporary files do not promise physical secure erase.
- [x] Scope subprocess environment to explicit required non-secret variables; disable ambient credentials, metadata, process providers, endpoint overrides, pager and auto-prompt. Keep exported keys in owned memory/IPC, never argv/environment or ordinary diagnostic output. STS uses the exact explicit tuple inside the isolated worker.
- [x] First login requires allowed interaction and a usable, positively acknowledged operation-owned private channel. Reject pre-canceled local owners and failed/unreachable remote opens before native helper admission; preserve retry ownership for any admitted open or close. A missing/disallowed owner fails before native login; [owned terminal handoff](implementation_plan_f4n1.md) can be supplied under NO_STD_IN. Present only login URL/code/output privately; route a direct-login authorization-code reply through owned stdin. Do not require machine parsing of undocumented human prompt grammar. SSO device handoff does not consume arbitrary secret input. Export/STS diagnostics never enter the channel.

## Interaction and lifecycle

- [x] Forward a generic borrowed interaction service through ordinary local and integration LLM owners. The adapter forks/owns only its own channels, preserving password-manager and sibling lifetimes. Reuse the private masked TUI channel with neutral authentication wording; do not persist its transcript or entered values.
- [x] Bound synchronous channel work and retain every pending thread/channel owner across cancellation, timeout and failed close. Channel and interaction close failures remain retryable, including remote positive acknowledgments and actual TUI dismissal; dispose successful siblings independently. Never acknowledge cleanup while pending helper work can still publish.
- [x] Own the complete native CLI/SDK/STS process group under shared timeouts and a child-local restrictive umask, preserving the caller environment and umask. Coalesce refresh through AcquiredSession. The original milestone reused only the adapter's ephemeral cache; the [persistence extension](implementation_plan_f4g.md) adds selected persistent cache reuse without allowing failed refresh to return stale credentials or another source. Revoke new work on close, drain/reap children and interaction before deleting owned files, and preserve retry owners on independent cleanup failure.

- [x] Repair reported copy UX: allow selecting read-only private authentication output and explicitly copying only that selection. Ctrl+C must copy or safely do nothing without cancelling authentication; keep masked replies private, deliberate cancellation visible, and existing lifecycle cleanup intact. Shared-screen and Run local assembly have focused independent qualification. External and human acceptance remain separate gates below.

## Runtime configuration and frozen credentials

- [x] Preserve exact checked credentials through requests. Support static frozen keys without a token/expiry, actual temporary expiry, and an explicit 60-second re-probe policy for token sources lacking known expiry. Never fabricate credential expiry. Pin refreshed ambient material to its initially verified account/principal. STS ignores configured endpoint overrides and disables proxies.
- [x] Preserve ConfigurationRequiredError fields through canonical dynamic adapter/session wrappers. Run local opens ordinary Required review with `?` help/edit/persist and retries only after local owned cleanup. No schema/help I/O. Other runtime consumer review routing remains unqualified.
- [x] Independently review ambient-first acquisition, refresh source binding, runtime review and controlled regression evidence before accepting this extension.

## Qualification

The preceding native snapshot passed 1,082 focused tests with exact100% line/branch
coverage across all13 executable provider modules (1,660 statements,544 branches)
and the two affected shared interaction/TUI modules (2,095 statements,690 branches
total). Three unchanged worktree-hook-warning stderr assertions are excluded;
full main-unit and live/native-CLI qualification remain separate. Actual shared
configuration defaults, parsed-flow/LocalLLM, integration-owner and private TUI
paths are exercised offline; blank reasoning omits its unsupported control.

The ambient-first extension passes 951 guarded provider tests at exact100%
line/branch coverage across all14 executable modules (1,805 statements,616
branches). Guarded verification blocks external network access in the Python
workers and isolates host AWS configuration. Final runtime review passes351 focused checks; independent Recheck/menu
qualification passes144 checks including21 private cases and12 causal controls.
Added enclosing statements and reported branches are fully covered; these checks
do not establish whole-TUI or full-main coverage.


- [x] Red-to-green focused tests cover both login methods, native commands/environment, temporary configuration, explicit identity/expiry checks, malformed frames, EOF, no secret diagnostics, dynamic loading and ordinary parser/LangGraph joins. Require exact100% line/branch coverage for all new production modules and meaningful causal controls.
- [ ] Complete independent implementation/integration review, verify source/wheel/sdist/installed/relocated resources and public boundaries, obtain exact external PR-style approval and human acceptance. Keep live account and inference acceptance separate.
- [ ] Stop for account/profile/permission readiness before any real authentication or catalog test. Qualify direct and SSO separately; obtain explicit bounded authorization before each new inference test. Offline process fixtures do not qualify actual native CLI transcripts or account behavior.

On 2026-10-24 the user reported successful direct browser/code handoff with the authentication-only success receipt. This qualifies the reported direct authentication handoff; that initial report did not qualify public inference. On 2026-10-25 the user reports the ordinary attended Bedrock integration test passes and Ctrl+V works, accepting the repaired direct sign-in interface and a nonempty completed request. Exact configuration/usage receipts, SSO and other mode qualification remain open. Neither report authorizes further live tests.

Public basis: [direct login](https://docs.aws.amazon.com/cli/latest/reference/login/), [SSO login](https://docs.aws.amazon.com/cli/latest/reference/sso/login.html), [credential export](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html), [login cache](https://docs.aws.amazon.com/sdkref/latest/guide/feature-login-credentials.html), [process credentials](https://docs.aws.amazon.com/sdkref/latest/guide/feature-process-credentials.html) and [STS identity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html). SDK support does not imply automated console authentication.

Follow-up: [owned credential readiness transactions](implementation_plan_f4a2a.md).
