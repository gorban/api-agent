# Native sign-in input handling

## Requirements and acceptance

- [x] Normalize ASCII copy-formatting whitespace in the native authorization code after enforcing the original input bound. Preserve standard Base64 characters, including +, / and = padding. Reject other characters with a fixed error; do not alter shared password-manager input semantics.
- [x] Distinguish native input closure, input frame limits, malformed frames, embedded line breaks and output limits with fixed protocol reasons. Retain no entered value or raw output in failure exceptions or public errors.
- [x] Preserve strict framing, byte bounds, process cleanup, cancellation, and pre-inference execution status. Keep unrecognized validation failures separate from recognized native CLI diagnostics.
- [x] Exercise actual Textual paste actions and the shared interaction IPC route using synthetic code and native subprocesses. Document terminal paste limitations without claiming successful authentication from synthetic tests.
- [x] Complete focused regression tests, full line and branch coverage for changed runtime code, causal controls and independent review.
- [x] Qualify fresh attended browser sign-in and a bounded real inference request through the ordinary launcher. Offline checks cannot establish the cause of an observed real failure.

The public [AWS CLI remote code parser](https://github.com/aws/aws-cli/blob/2.38.5/awscli/customizations/login/utils.py) decodes standard Base64. Normalization is limited to ASCII space, tab, CR, LF, vertical tab and form feed; it does not decode, log, repair padding or rewrite other characters.

## Interaction limits

Use the fresh URL and code for the current launcher attempt. The shared private input follows the [clipboard-origin requirements](implementation_plan_a9k2.md): Ctrl+V reads the operating-system clipboard, while terminal Paste delivers its complete payload. On Linux, terminal Paste is commonly Ctrl+Shift+V. Native code normalization joins ASCII formatting whitespace after the complete input reaches the provider. Never submit authorization codes or login URLs in test reports.

On 2026-10-25 the user reported that Ctrl+V works and the existing attended Bedrock integration test passes through the ordinary launcher. This accepts the repaired direct sign-in/paste path. Other authentication modes and exact configuration/token-usage receipts remain separate.
