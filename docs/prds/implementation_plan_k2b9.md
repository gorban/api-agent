# Local startup failure diagnostics and account selection

[Local startup and shutdown](implementation_plan_k2b.md)

- [x] Deliver startup failures into the selected agent log and All logs, with a newline, a useful failure category and safe actionable context. Preserve the original failure and ordered cleanup if diagnostic delivery itself fails. Stage notices alone do not satisfy failure reporting.
- [x] Preserve useful listener failure causes without terminating the coordinator loop, capturing unrelated process logs or exposing credentials. Distinguish a known bind error from an unknown startup exit; never stop another listener automatically. Verify with an occupied ephemeral loopback port and controlled startup failures.
- [ ] Before interactive Run local startup, make the selected attended/unattended account explicit and allow changing the index using the existing account-selection and configuration provenance rules. An unattended index is optional. Carry the selected identity to both enabled LLM and UI consumers; selection must not authenticate by itself.
- [x] Preserve lazy attended session authentication: reuse valid cached credentials first, then use the selected session provider and existing interaction owner for recovery. Never switch identities or require an unattended account to recover an attended session. Noninteractive missing interaction must remain actionable.
- [ ] Preserve local cleanup, cancellation and status ownership; cover source and installed behavior, applicable exact coverage, independent/external review and human acceptance. Retain an unresolved initiating cause explicitly if the original failure cannot be reproduced.

Startup diagnostic delivery and listener classification passed independent and external review and controlled failure tests. Human review accepted the controlled failed header, startup stage logs, actionable listener diagnostic and error box. Lazy attended authentication preservation was verified. The original initiating cause and the pre-start account selector remain open. Unit scratch storage is tracked under [full unit execution](implementation_plan_a9i4.md); other acceptance remains separate.
