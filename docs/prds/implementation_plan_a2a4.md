# Login-only recovery guidance

[Password-manager requirements](implementation_plan_a2a.md).

- [x] After a typed login-required failure, ordinary username/password/TOTP retrieval may use the adapter's optional login probe. Require the exact resolved executable plus `whoami` before opening an interaction or process. Selected-login discovery shares that validation. Adapters without a probe retain credential-private recovery behavior.
- [x] Only an explicitly validated login-only probe may forward stdout guidance through the private recovery interaction. Keep stdout piped and stdin/stderr attached to the existing PTY. Make only the login-only child unbuffered so ordinary unflushed guidance arrives before user action; preserve inherited environment and private-command buffering. Keep credential-result stdout, exported records and entered secrets out of recovery output, ordinary stdout, diagnostics, logs, traces and artifacts.
- [x] Apply independent byte limits and incremental terminal-control/UTF-8 handling to stdout guidance and PTY output. Preserve cancellation, retry generations, EOF, process disposal and stream closure.
- [x] After successful login, retry the original credential command through private capture using its remaining short execution budget, excluding human interaction time. Do not reset the enclosing recovery deadline, account identity or session ownership.
- [x] Verify unsafe-probe rejection causally; prove real synthetic helpers show guidance before input with unchanged TTY behavior, preserve original credential privacy, independent stream filtering/limits and owned cleanup. Complete applicable coverage and independent review.
- [ ] Obtain human acceptance of the reported hardware-touch guidance. Synthetic verification does not claim live authentication or change an account's PIN/session state.
