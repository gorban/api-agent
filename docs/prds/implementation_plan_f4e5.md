# Native CLI failure categories

## Requirements and acceptance

- [x] On unsuccessful native command exit, classify complete known diagnostic signatures in the existing bounded output into fixed invalid-code, state-mismatch, expired-code, certificate, connectivity, or permissions categories.
- [x] Retain only fixed kind/reason values in native failure exceptions and acquisition protocol; keep diagnostic text, authorization values, URLs, and dynamic service messages out of public errors.
- [x] Keep unknown, partial, conflicting, or unrecognized diagnostics unknown. Preserve successful exits even when their output contains a recognized marker.
- [x] Validate the new protocol kind/reason pairs strictly and provide fixed actionable guidance for the new code and permissions reasons. Preserve inference execution as not started.
- [x] Preserve native input handling, source guards, output limits, cancellation, and process cleanup. Do not automatically answer consent prompts or change certificate trust or proxy configuration.
- [x] Independent review and offline validation of native failure classification and visible error handling.
- [x] Human acceptance of browser sign-in. A generic native failure does not establish whether the browser code, network, permissions, or another cause failed.

## Contract and evidence

Recognition uses the public AWS CLI 2.38.5 [remote sign-in parser](https://github.com/aws/aws-cli/blob/2.38.5/awscli/customizations/login/utils.py), [exception formats](https://github.com/aws/aws-cli/blob/2.38.5/awscli/botocore/exceptions.py), and [error formatting](https://github.com/aws/aws-cli/blob/2.38.5/awscli/errorhandler.py). The 2.38.5 [error writer](https://github.com/aws/aws-cli/blob/2.38.5/awscli/errorformat.py) adds the exact `aws: [ERROR]: ` prefix and strips message whitespace. Recognition accepts that prefix and the unprefixed format documented by the supported 2.32.0 [error handler](https://github.com/aws/aws-cli/blob/2.32.0/awscli/errorhandler.py). Every signature is tested in both formats. State-field extraction uses linear string operations, with maximum-output adversarial cases under a subprocess deadline. Dynamic fields are discarded; matching is diagnostic evidence rather than proof of service authenticity. General exceptions use plain text even when structured error output is configured; both legacy and enhanced permission headers are recognized. Unknown versions and unsupported messages retain the unknown fallback.

Offline tests cover every signature, duplicate-category markers, conflicting categories, malformed lookalikes, strict transport validation, fixed exception state, chunk boundaries, and actual synthetic child success/failure. No live account or native AWS CLI is required.

The focused native failure, diagnostics, worker, transport, ambient, launch, and admission suites pass 308 tests. Worker and transport both have 100% line and branch coverage (328 statements, 136 branches total). Focused mutation controls reject removal of signature recognition, bypassing classification at the nonzero-exit boundary, accepting conflicting signatures, bypassing exact writer-prefix handling, dropping state classification, and reintroducing the backtracking parser; originals were restored after each control.

On 2026-10-25 the user reported that Ctrl+V works and the existing attended Bedrock integration test passes through the ordinary launcher. This accepts the repaired direct sign-in/paste path. Other authentication modes and exact configuration/token-usage receipts remain separate.
