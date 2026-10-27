# Cached native sessions and authentication copy hints

[Bedrock requirements](implementation_plan_f4.md) · [Startup repair](implementation_plan_f4h.md) · [Private interaction](implementation_plan_a9k2.md)

The user reports a successful attended session inference after fresh browser login, followed by a second-run credential-resolution failure instead of cached reuse. Attended access-key entry successfully asks for key ID and secret; selected unattended access-key inference remains successful. These reports do not establish cached reuse acceptance.

- [x] Reproduce and repair SDK native-login cache reuse after persisted login and snapshot masking. Include required SDK optional dependencies in both the public Bedrock extra and selected-provider dependency declaration; missing dependencies must produce safe prerequisite guidance, never an opaque unknown failure or another credential source. Preserve native refresh, selected identity, credential snapshot and lifecycle contracts.
- [x] Verify the default-profile shared-credentials snapshot and permitted model discovery without inference. Separate snapshot usability from refreshable native cache reuse. Keep credential values, account identifiers, raw SDK/CLI diagnostics and authorization material out of recorded evidence.
- [x] Replace the authentication screen's dedicated output-copy button with the ordinary highlighted ^c footer shortcut. Show its hint only while output text is selected. Preserve priority Ctrl+C consumption without selection, prevent secret-input copying and keep ordinary cancel/retry behavior. The shared password-manager screen receives the same behavior; see the generic private-interaction requirements.
- [x] Complete focused regression/causal checks with exact new-code line/branch coverage, distinct low implementer/reviewer checks, boundary/package qualification and external PR-style review before another human retry.
- [ ] Human acceptance: sign in through ordinary launch.py when needed, complete the selected session test and repeat/restart without a new login. Confirm output selection controls the ^c hint and secret selection never exposes a copy action. Existing root inference spending approvals are exhausted; non-inference discovery is authorized for this diagnosis.

## Reproduction and bounded verification

The actual SDK raised MissingDependencyException because CRT was absent. CLI
login succeeded using its separate bundled runtime. Both Bedrock installation
paths now request Boto3's crt extra; the actual missing-dependency exception maps
to setup prerequisites. A synthetic persisted login uses the real SDK resolver
and public cache schema, with causal controls for prerequisite mapping and snapshot
masking. It does not independently exercise CRT signing or network refresh.

Before the dependency repair, the exported snapshot had expired and its direct
model-list request was rejected. After installing CRT without changing the
existing Boto3/Botocore versions, native readiness succeeded with browser recovery
disabled, refreshed the snapshot, and succeeded again in a fresh process. The
default-profile CLI identity check and a Mantle model list signed with the exact
exported tuple then passed; 60 entries included OSS20B. No inference was invoked.
Real public-launcher restart acceptance remains the separate human criterion.

## Focused qualification

The final guarded related suite passed 1,418 tests with exact 100% line and branch
coverage for the entire Bedrock provider and shared authentication screen: 2,481
statements and 834 branches. Distinct lane reviews and causal controls passed.
The initial expanded run exposed four stale test cases; tests now use a valid
synthetic Base64 native reply and isolate unrelated real hook assurance, retaining
their dispatch/privacy assertions. The clean final run follows those fixture
repairs. Boundary checks, rebuilt wheel/sdist resources and installed/relocated
probes passed. External PR-style review approved the bounded candidate, including
four independently repeated offline checks. No new inference was run. Ordinary
launcher repeat/restart and visible copy-hint acceptance remain unchecked above;
full-main acceptance remains separate.
