# Included Studio frontend and adapter

[Frontend integration](implementation_plan_i2.md) · [Reasoning controls](implementation_plan_f2f.md) · [Completion bridge](implementation_plan_i2b.md)

Deliver the included UI through the generic provider/host contracts. Controlled browser tests cover presentation and failure behavior before the
real-listener test exercises the selected provider with one small prompt. This
does not certify workflow execution or the full application E2E runner.

## Presentation and state

- [x] Preserve the single-page warm light and Midnight dark themes, System/Light/Dark preference, early appearance application and cross-tab preference updates.
- [x] Keep a scrolling conversation and stationary composer, responsive welcome, keyboard operation, visible focus, reduced motion and accessible status announcements.
- [x] Preserve streamed Markdown, Stop/retry, persisted conversation/draft, New chat/Undo, immutable requested selections, reported response metadata and follow-latest controls.
- [x] Render assistant Markdown with pinned local Marked and DOMPurify bundles. Preserve their exact notices/licenses, the font license and third-party inventory. Sanitize before insertion, keep user prompts literal, and prevent passive external-image requests and active response HTML.
- [x] Support real structured clarification questions with editable suggestions. Choosing a custom answer clears only an unchanged suggested option; preserve user edits and restore the pre-question draft after submission. Keep approval/rejection explicit, with no invented backend success or automatic approval.

## Provider and transport

- [x] Package frontend resources with an included replaceable UI provider and production host. Use explicit asset routing, bounded configuration, verified readiness and owned shutdown; exclude standalone preview scripts, generated output, labs and synthetic response resources.
- [x] Normalize catalog selections against actual public provider model/version/effort capabilities. Preserve omitted/default versus explicit off, reject stale/unsupported choices before sending and retain requested versus reported identity without fabricating metadata.
- [x] Define and test the browser adapter boundary against the agent's available contracts, including JSON/POST-SSE framing, error/EOF, cancellation, liveness and continuation. Explicit deferred HTTP responses remain errors rather than fabricated replies; do not claim live compatibility until real handlers are implemented.
- [x] Verify controlled HTTP and browser interactions across success, partial failure, abort, retries, persistence and clarification without external inference, real credentials or history changes.

## Delivery gates

[Parallel inspection ownership](implementation_plan_i2a1.md) records the
creating-worker retention and explicit cleanup checkpoint.

- [x] Require meaningful unit/frontend assertions and focused mutations, exact applicable line/branch coverage, installed relocated-resource loading, host/browser lifecycle tests, license checks and independent adversarial review.
- [x] Preserve the original worker-owned DEBUG UI browser after parallel test completion. Define controller ownership/handoff before implementation; retain the creating loop, report inspection endpoints and finalize test evidence before user-driven browser closure. Prove cancellation, worker loss, cleanup retries and no resource leaks through actual parallel runners. Serial controller inspection is verified but does not close this requirement; resolve it before application E2E.
- [x] Audit independently authored source, intended index and actual commit metadata while preserving unrelated changes. Keep private handoff/provenance outside this repository.
- [x] Human review of the joined provider/theme/settings/continuation behavior, deferred until working Run local or [retained interactive Studio](implementation_plan_i2c2.md). Actual listening agent, authentication, inference and first application E2E retain their owning application gates.

## Verification checkpoint

The joined Python gate passes 8,904 units at exact 100% measured line/branch
coverage. Authored frontend checks pass 111 cases plus five reply controls;
browser modules use static line/branch measurement and Node modules use native
V8 line/branch measurement. Installed assets, notices and provider discovery
resolve outside the checkout. Controlled HTTP/browser tests include connection
failures. A minimal live browser-to-agent-to-provider request returned the
expected text. That earlier checkpoint left parallel UI DEBUG retention open. The
[parallel ownership follow-through](implementation_plan_i2a1.md) now closes that
implementation gap with independent controlled proofs; full application E2E and
workflow execution retain their separate gates.

- [x] The compact composer model indicator appends `: <reasoning>` for every explicitly selected effort, including off; provider-default effort has no suffix. Preserve model/version selection and immutable per-message metadata.

## Keyboard send after conversation reset

- [x] Preserve selected model/version/effort across New chat. A typed draft sent with Ctrl+Enter uses that exact selection and only the fresh conversation's messages.
- [x] Present unsupported or unknown selection capabilities beside the composer when sending is blocked, retain the typed draft, and make the reason accessible through the existing prompt description. Do not silently choose a capability or manufacture a request.
- [x] Keep active stream exclusion, Stop, New chat and Undo coherent. A deliberate reset clears the draft; Undo restores it until the next accepted send. Corrected advertised capabilities allow the preserved selection to send.
- [x] Independently review the controlled browser regressions and complete joined verification. Human review remains open.

- [x] When a chosen family has an unresolved Provider default version, offer only reasoning efforts supported by every advertised row with verified reasoning support. Unknown support yields no explicit choices. Preserve version rejection without choosing a fallback; empty reasoning inherits configured effort, while empty configuration remains omitted upstream.

Human acceptance of retained Studio is recorded; the new reasoning and visible composer-rejection follow-ups remain separate. Long-idle cache review is not accepted by this signoff.

- [ ] Human review of the new reasoning choices and composer rejection presentation. Prior retained Studio acceptance remains recorded.

[Operation-specific browser visibility](implementation_plan_i2a2.md) defines Run local frontend selection and debug-only test browser visibility.
