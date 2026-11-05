# Loopback binding and advertised readiness identity

[Prepared UI lifecycle](implementation_plan_i2c1.md)

## Requirements

- An injected UI provider may supply an optional `readiness_origin` to the
  owned process launch service. It identifies the advertised HTTPS authority,
  independently of the admitted loopback socket host.
- Require an exact canonical ASCII origin with the same HTTPS scheme and port
  as the launch socket, no user information, path, query, fragment or whitespace.
  Reject invalid input before launching a child. Existing calls retain their
  original behavior when this argument is omitted.
- Connect only to the admitted loopback address. Do not resolve or connect to
  the advertised DNS hostname. Send its authority as HTTP Host and its hostname
  as TLS server name, retaining required CA trust and certificate hostname checks.
  Never follow redirects or use ambient proxy configuration for readiness.
- Preserve launch-token identity verification, bounded readiness, cancellation,
  child ownership and retryable cleanup. A readiness response does not authorize
  browser requests, mutate host DNS/trust, or establish external login success.
- A trusted `StudioBackend` constructor may supply the same optional advertised
  browser origin separately from its admitted loopback origin. Require canonical
  HTTPS with matching port; keep exact advertised Origin, secret nonce and
  loopback peer checks on every request. Direct named origins without this
  explicit construction contract remain rejected. The shared validator grants
  neither authentication nor remote network access.

## Acceptance

- [x] Controlled real TLS tests prove named-host readiness over a loopback-only
  connection, including retries after a non-ready response.
- [x] Reject untrusted CAs and mismatched certificate names without admitting
  readiness; preserve existing launch/disposal controls.
- [x] Complete adversarial review, causal rejection controls, exact applicable
  unit coverage and normal repository/boundary gates.
- [ ] Human acceptance with a selected provider's advertised endpoint. Browser
  DNS, certificate provisioning and provider-specific callback remain its own
  setup requirements.
