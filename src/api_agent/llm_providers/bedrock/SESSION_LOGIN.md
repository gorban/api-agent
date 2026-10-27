# Selected-profile sessions and attended recovery

[Provider setup](README.md)

For an attended session select:

    BEDROCK_AUTH_MODE=session
    BEDROCK_SESSION_PROVIDER=src/api_agent/llm_providers/bedrock/aws_login.py:factory

The selected session factory controls recovery. Use aws_login.py:factory for
direct AWS console sign-in or aws_sso.py:factory in the same directory for IAM
Identity Center. Both share credential verification, persistence and cleanup.
Relative selectors use the provider owner root; absolute paths also work. For an
installed package use the corresponding installed adapter file's absolute path.
Builtin/custom adapters retain exclusive acquisition ownership; native settings
are consumed only by these explicitly selected bundled adapters.

The earlier PoC BEDROCK_LOGIN_METHOD setting is obsolete and may be removed from
.env. Migrate its sso value by selecting
src/api_agent/llm_providers/bedrock/aws_sso.py:factory as BEDROCK_SESSION_PROVIDER;
the existing aws_login.py:factory selector always means direct recovery.

Clear UNATTENDED_USER_INDEX: indexed automation is rejected before credential
resolution. Both builtin session selection and the bundled native adapter first
resolve the selected SDK profile/chain in an owned bounded process and verify
readiness with STS. The exact frozen key/secret/token tuple is checked and passed
to Mantle; SDK resolution never occurs during help or configuration review.
Valid credentials require no extra identity settings, CLI or browser handoff.

BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID and BEDROCK_LOGIN_EXPECTED_PRINCIPAL are
optional independent assertions for the native adapter, applied to both ambient
and freshly acquired credentials. The principal is an exact IAM user ARN or
stable STS assumed-role prefix without the final session-name segment. A shared
role proves that principal, not a particular human. After selecting ambient
credentials, refresh stays bound to the verified account/principal; failure
requires reauthentication and a new session, with no subsequent native fallback.
Temporary credentials use their actual SDK expiry. Token-bearing sources without
known expiry are re-probed every 60 seconds; this is a readiness policy, not an
invented expiration. Static credentials have no fabricated expiry.

An absent selected SDK profile counts as missing credentials; native recovery
creates that same profile name in its isolated files and never selects another
SDK profile.

Only recognized missing, expired or invalid credentials allow the explicitly
selected native adapter to start attended recovery. Network failures, denied
access, missing dependencies, malformed profiles and other errors stop without
login. The builtin adapter never starts CLI recovery; custom adapters retain
exclusive acquisition ownership. Mantle authentication/authorization or inference
failures never trigger a browser or another credential source.

For IAM Identity Center select aws_sso.py:factory; this is the native AWS
device handoff, not an Okta browser adapter. Recovery uses available
routing from the selected profile, with optional explicit overrides:
BEDROCK_LOGIN_SSO_START_URL, BEDROCK_LOGIN_SSO_REGION,
BEDROCK_LOGIN_SSO_ACCOUNT_ID and BEDROCK_LOGIN_SSO_ROLE_NAME. The SSO account
is a routing input, separate from the expected-account assertion. Missing routing
is requested only after ambient credentials prove unusable. In Run local, a
fields-only ConfigurationRequiredError opens normal Required configuration review
with `?` help and ordinary edit/persist behavior; closing review retries only
after local cleanup. Other runtime consumers' review routing remains unqualified.
After verified native acquisition, the adapter persists only the selected
profile/session/cache entries and its exported snapshot as described below.
Ordinary SDK refresh and configured credential_process helpers can also have
their own cache/helper effects.

Native recovery starts in an isolated profile and private HOME/config/credentials,
login and SSO caches; it copies only the selected routing and recognized cache
entries, rather than arbitrary profile contents. Recovery requires nonblank profile/region, normally supplied by application
defaults, and AWS CLI v2 >=2.32.0. Direct login uses aws login --remote, receiving
its authorization code through the private masked screen. SSO uses a device
URL/code handoff. Complete browser sign-in yourself; automated form filling is
not implemented. NO_STD_IN or a missing owner rejects recovery, while still
allowing valid ambient credentials.

Exported native credentials require token and future expiry. STS uses the exact
tuple, with configured endpoint overrides and proxies disabled, and checks any
optional assertions before publication. Successful native login and refresh then
persist both refreshable selected AWS profile/login or SSO cache settings and the
exported access-key/secret/session-token snapshot in the configured shared
credentials profile (normally ~/.aws/credentials). Failed or unverified acquisition
is never published. No additional package is needed for persistence beyond the
existing native AWS CLI v2 prerequisite.

Later launches recognize their own unchanged exported snapshot. When its recorded
expiry is more than 30 seconds away, both builtin and native adapters verify and
reuse that exact tuple, independently of native cache availability. The exported
tuple is the request credential source during this valid period; native profile
and cache state supplies renewal near expiry. Native renewal masks only the
provider-owned selected snapshot in an isolated input file. The builtin adapter
never starts attended recovery. External SDK/CLI consumers may read the static
snapshot until it expires; they do not implement these adapter renewal rules. User-edited tuples are preserved
and are not silently replaced or used to trigger another credential source.
Native refresh stays bound to its verified identity.

Selected-file updates preserve unrelated profiles/settings, use private
permissions, a cooperative lock, concurrent-change checks and per-file atomic
replacement, with rollback on publication failure. This is not filesystem-wide
atomicity; uncooperative external writers can cause an explicit failure. Blocking
helpers and file work stay in owned bounded workers. Closing drains process
groups/channels before deleting temporary files; persisted selected entries
survive disposal, and cleanup failure retains retry ownership. Temporary storage
does not promise physical secure erase.

Indexed session acquisition remains separate future work for both direct AWS
and replaceable federated adapters, including Okta. Those adapters must preserve
the selected identity and requested TOTP behavior. Attended access_key/api_key
prompts and the native Identity Center device handoff do not implement that
automation.

The initial direct handoff has reported human acceptance. Normal default-profile reuse has reported human acceptance. Snapshot-only reuse,
SSO, native CLI interoperability and broader account/model qualification need
separate evidence. The session-reuse follow-up passed focused independent/external review and
package/resource/public-boundary checks. Human acceptance remains open; offline
tests do not qualify a real persisted login across processes. Broader native
interoperability and main-adoption gates remain separate. See the [native sign-in requirements](../../../../docs/prds/implementation_plan_f4a2.md).


Native SDK cache reuse requires the Boto3 CRT extra, included in the Bedrock
installation declarations as boto3[crt]. Installing AWS CLI alone does not supply
this library to the application Python environment. Missing CRT is reported as a
setup prerequisite; it does not switch credentials or start another login.
