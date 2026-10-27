# Provider-owned integration test profiles

An LLM provider may include `test-profiles.toml` beside its selected factory.
The version-1 declaration names that factory symbol, declares its API/generation
field allowlist, and groups defaults under named profiles containing exact
`tests/integration/file.py::test_case` identities. Parameterized cases require
exact identities; file prefixes and wildcards are not selectors. Declarations
are read without importing the provider or acquiring credentials.

The launcher resolves every admitted case before creating any provider/session.
Precedence is application configuration, then provider test defaults, then
explicit test-specific `LLM_*_TEST_*` settings. Explicit empty test choices remain
empty and fail their prerequisite instead of receiving a hidden replacement.
Application API/model choices retain their application scope. Dict-only callers
without source provenance conservatively retain all supplied test-specific keys.

Profiles may change validated shared model, effort, workflow and timeout fields,
and provider-declared API/generation controls. Provider selection, credentials,
authentication identity, allowed-model policy, environment, dotenv and security
settings cannot be overridden. Exact test model choices still obey
`LLM_ALLOWED_MODELS`; catalog capability assertions remain mandatory.

Workers receive only the host-admitted exact-case configuration map. Each test
gets a fresh local copy and a callable owner; identical effective configurations
may share an owned host setup backend. Different effective configurations have
separate backends. No worker changes a shared application configuration, and
profile order/concurrent requests cannot select another case's route. Existing
request and user-turn attribution and cleanup leases remain in force.

The effective values and their profile/explicit provenance are available for
review before execution and recorded with the test. Provider-specific profile
values belong in provider declarations, rather than protocol branches in generic
test drivers. The calibration command resolves the same workflow-case profile
before fingerprinting. Unsupported selections require a supported profile first;
missing/stale baselines require an explicit fresh, reviewed calibration. Older
reports are retained and never relabeled for a new source fingerprint.

Acceptance requires inert admission, exact identities, explicit empty semantics,
allowed-model enforcement, identity/environment invariance, order/concurrency
isolation, backend/worker timeout consistency and source-bound workflow evidence.
Live qualification remains a separate explicitly authorized operation.
