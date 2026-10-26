# UI evidence after each user turn

[Test-owned UI evidence](implementation_plan_a4a.md) · [Conversation reporting](implementation_plan_b1c1a2b.md)

UI-driven tests must preserve the displayed response before sending the next
prompt. A server-side graph ending does not establish that the browser finished
rendering that response.

- [x] Provide one shared awaited test-turn scope, independent of frontend DOM.
  Serialize prompt submission, response readiness and screenshot completion for
  the same prepared UI session. Included Studio request helpers use this scope;
  other UI providers can use it with their own readiness checks.
- [x] Bind snapshots to the actual request's canonical conversation and turn
  identities. Capture after the caller observes response rendering, including
  available failure states, before releasing the next test prompt. Do not infer
  identities from timestamps, display labels or the last graph in a test.
- [x] Use the existing selected-provider original-pixel safety policy, live
  credential authority and LOG_CONTENT permission. TRACELOOP_TRACE_CONTENT does
  not authorize screenshots. Disabled content performs no browser inspection.
- [x] Bound capture duration and cumulative evidence. Await completion of the
  bounded attempt; unavailable, unsupported, rejected or timed-out capture stays
  explicit and never replaces the primary test result. Preserve cancellation
  and ownership without starting detached capture tasks or retry loops.
- [x] Store each immutable screenshot once and attach its reference beneath the
  matching User turn after final accounting/grouping. Unknown or ambiguous owners
  remain explicitly Unattached. Preserve legacy final UI context, especially for
  UI-only tests without user turns, and existing withholding notices.
- [x] Verify two responses and a new conversation, capture-before-next-submit,
  error/timeout/cancellation, missing owners, reordered evidence, final regrouping,
  cumulative bounds and both content flags independently. Include actual browser
  capture with a deterministic provider and normal immutable recording; no paid
  inference is required.
- [x] Complete applicable exact unit coverage, installed/package and boundary
  checks, independent reviews and external review before committing.
- [ ] Human review confirms snapshots show each response before the next prompt
  and remain under their correct User turn in newly recorded Allure results.

## Final-context ownership refinement

- [x] After an accepted turn PNG, omit the redundant final success capture of
  that same prepared page. Preserve other owned pages and their context/page
  numbering. UI-only tests without a captured conversation keep their top-level
  success context. Missing, rejected or unavailable latest turn capture keeps
  the final fallback; terminal failure context remains available.
- [x] Preserve genuine snapshots from different turns even when pixels match;
  deduplication follows capture ownership, not image-byte equality.
- [x] Verify normal recording placement, multiple pages, original pixels,
  publication failure and independent content permissions; finish exact
  applicable coverage, package/boundary and independent/external reviews.
- [ ] Human review newly recorded turn-based and UI-only screenshots.

**Deferred human review**

Implementation and required verification/reviews are complete. Explicit human
acceptance above remains unchecked, including newly recorded screenshot placement.
