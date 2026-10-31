# Portable local certificate validation

[Local HTTPS preparation](implementation_plan_i.md)

- [x] Keep mkcert pinned in the project mise configuration and prepare it only at
  the existing HTTPS boundary. Prefer that selected executable over an installed
  system copy. Reuse the normal local CA; do not reset trust or generate a new CA
  merely to change executable versions.
- [x] Support the host OpenSSL-compatible CLI, including LibreSSL, without relying
  on the OpenSSL-specific `x509 -ext` option. Read only the subject-alternative-name
  extension from portable certificate text. Never accept subject/issuer text,
  unrelated extension values, duplicate or missing SAN blocks as hostname proof.
  Admit complete DNS/IP GeneralName entries only; unsupported entries or malformed
  IP values reject the certificate rather than becoming hostname evidence.
- [x] Retain renewal/expiry, DNS versus IP identity, public/private key matching,
  CA verification, private permissions, locking and recoverable atomic replacement.
  No failed validation may install or leave a mixed certificate/key pair.
- [x] Report a fixed, non-content validation stage when generated material fails.
  Do not expose command output, certificate contents or private-key material.
- [x] Verify compatible command shape, generation/reuse, malformed text rejection,
  independent validation failures and causal controls. Close applicable coverage,
  installed/relocated artifacts and independent/external review before delivery.
- [ ] Human verify HTTPS preparation and browser trust on the affected macOS host.

Mise supplies the pinned mkcert executable. Host OpenSSL/trust and account
permissions retain their existing prerequisite policy; a working system mkcert is
not a substitute for selected-version preparation.
