# Acquisition failure diagnostics

Bedrock sign-in failures must expose a useful fixed diagnostic through the ordinary setup UI while retaining credential isolation and the selected identity.

## Requirements

- [x] Distinguish credential resolution, native sign-in, credential export, identity verification, result validation, and worker protocol stages using a closed stage vocabulary.
- [x] Report only normalized TLS, connectivity, rejected, malformed, or unknown reasons and fixed operational categories. Never expose child output, SDK exception messages, response bodies, profile/account/principal values, credentials, or request identifiers as diagnostic evidence.
- [x] Preserve safe acquisition evidence through adapter/session ownership and the shared setup transport. Acquisition failures must retain `not_started` inference execution status, including unknown failures.
- [x] Keep prerequisites and interaction requests distinct. Identity and transport failures fail closed and cannot initiate an alternate login or replace the selected identity.
- [x] Independently review the implementation and focused offline regression evidence.
- [ ] Human acceptance: an ordinary sign-in failure displays its safe stage and reason with actionable context.

## Validation

Synthetic worker failures, protocol rejection, adapter/session propagation, and shared setup roundtrips exercise diagnostics without live cloud access. Existing credential and lifecycle tests remain applicable. A sanitized stage identifies where failure occurred; it does not establish the cause of a previously reported failure.
