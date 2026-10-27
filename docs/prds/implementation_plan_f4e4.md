# Native authorization-code clipboard input

## Requirements and acceptance

- [x] The bundled native sign-in interaction accepts a browser authorization code with trailing clipboard CR/LF characters and forwards the code as one line.
- [x] Remove only trailing CR/LF at this native-code boundary; preserve spaces and internal characters. Keep worker framing and rejection of embedded CR/LF or multiple code lines strict.
- [x] Preserve existing empty-input behavior: an empty or newline-only submission remains an empty code for the native CLI to handle.
- [x] Keep shared password and other interaction input unchanged.
- [x] Verify with synthetic input and subprocesses; no live account is required.

## Evidence

The native interaction removes trailing CR/LF only, after validating the original input bound. Empty input remains empty; newline-only input becomes empty and follows the existing empty-code handling. Shared input and worker validation are unchanged.

Focused interaction, worker, transport and admission suites pass: 151 tests; the modified interaction module has 100% line and branch coverage (98 statements, 26 branches). Removing only normalization makes all four trailing-line-ending regression cases fail. Synthetic tests preserve spaces and embedded CR/LF so worker rejection remains applicable.

- [x] Independent review of normalization and regression coverage.
- [x] Human acceptance of a browser clipboard code through native sign-in. A clipboard paste can retain a trailing newline; this identifies a reproducible failure route without establishing any particular user's pasted contents.

On 2026-10-25 the user reported that Ctrl+V works and the existing attended Bedrock integration test passes through the ordinary launcher. This accepts the repaired direct sign-in/paste path. Other authentication modes and exact configuration/token-usage receipts remain separate.
