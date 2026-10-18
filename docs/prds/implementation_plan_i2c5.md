# Studio application HTTP transport

[Frontend foundation](implementation_plan_i2.md) · [Included Studio](implementation_plan_i2a.md)

- [x] Consume the shared application model DTO with exact backend default selection, advertised metadata and explicit reasoning omission versus off. Preserve valid saved selections and reject stale capabilities.
- [x] Send fresh operation session identifiers, model and role/content history. Preserve immutable requested selections, actual returned identity, draft, retry and accessible presentation.
- [x] Parse bounded named SSE incrementally, binding every lifecycle frame to its admitted session and enforcing completion ordering. Reject malformed, oversized, failed, cancelled and incomplete streams without synthetic success. Retain optional question presentation while continuation remains unavailable.
- [x] Send periodic admitted-session heartbeat requests and explicit Stop/pagehide cancellation with configured nonce admission. Abort the transport and clear owned timers. Tab visibility alone must not cancel. Page exit including BFCache uses cancellation; abrupt loss is bounded by backend expiry. Maintain an absolute 150-second browser safety deadline aligned with the backend request and cleanup limits; heartbeat cannot renew it.
- [x] Verify controlled browser/HTTP Stop, pagehide, visibility, heartbeat, cancellation failure and fresh retry identity. Preserve existing rendering, keyboard, storage and metadata regressions; no real credentials, user history or paid inference.
- [x] Complete independent review, applicable exact frontend coverage and public-boundary checks before delivery.
- [ ] Interactive human acceptance remains deferred to a working retained Studio session.

## Controlled verification

The authored frontend gate passes 119 cases plus five reply controls. Authored browser modules and Node modules have exact 100% measured line and branch coverage. Independent review and source-boundary checks pass. Browser navigation and tab closure close the HTTP transport; keepalive cancellation is best effort and may be lost on exit, so backend disconnect handling and lease expiry remain required.

Terminal SSE events require a real blank-line delimiter. EOF does not supply a missing delimiter: unterminated done frames remain incomplete and retryable. Controlled transport and actual HTTP/browser checks cover all three terminal truncations, complete LF/CRLF/CR streams, trailing comments, question presentation and incremental text.
