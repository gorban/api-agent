# Local-run navigation and catalog review repairs

[Core operation](implementation_plan_h1a1.md) · [Startup tabs](implementation_plan_k2.md)

- [x] Replace Browser destinations and Log sources typed-number text menus with shared-style, focusable buttons. Enter activates the focused choice, including the initially selected first choice; mouse activation works too. First ten choices have immediate hotkeys 1 through 9 then 0. Remaining choices use focus navigation without concatenated numeric input.
- [x] Support arrows and standard Vim directions: h left, j down, k up, l right. Preserve focus/selection across live status and log refreshes. Closing a menu returns to the same running operation. Menu navigation keys must not accidentally open another menu, and displayed read-only logs must not present an editable menu cursor.
- [x] Render destination name and URL on separate button lines with wrapping and useful focus styling. Ctrl+C explicitly copies the focused destination URL; on log-source buttons it copies the source name. Reuse the existing safe clipboard implementation and feedback. Navigation/refresh alone never writes to the clipboard; copy failures preserve the menu and operation.
- [x] Start and foreground the enabled prepared UI last in the original managed browser. Core order is home, actual selected flow, UI; without UI, flow is last. Reserve the following tracing order as home, enabled tracing viewer, actual flow, enabled UI. Preserve exact owned-tab checks, replacement-page readiness, graph identity and cleanup receipts.
- [x] Make catalog loading visible and bounded by the supported backend operation budget rather than a shorter first-page-only timeout. A slow initial provider setup must populate the real model/version/reasoning choices without reloading the page or fabricating choices. An actual failure must remain actionable and permit explicit retry without restarting the local operation or duplicating authentication.
- [x] Prove focused-button activation, tenth/later choices, Vim/arrows, wrapping, refresh stability, explicit copy and failure handling through actual Textual events. Prove tab order/foreground with original and replacement UI pages. Reproduce delayed catalog setup and genuine errors with controlled HTTP/browser checks, including retry and request cancellation. Preserve privacy and no eager inference.
- [x] Complete meaningful causal controls, exact applicable coverage, source/installed artifacts, independent lane reviews, public-boundary checks and external joined review. Do not use live inference merely to test startup/catalog behavior.
- [x] Human review of menus, clipboard, reordered tabs and working Studio catalog/chat is recorded in the October 23, 2026 Run local approval. Earlier accepted scopes retain their approvals.

## Catalog cache status and age

- [x] Never reuse failed or cancelled catalog retrievals, including invalid provider catalog results. A later retrieval may try again and fail again; no automatic retry loop or exponential backoff is introduced. Preserve sticky authentication/setup failures and the original selected session/account owner.
- [x] Cache only validated successful catalogs. Set the visible `LLM_CATALOG_CACHE_TTL_SECONDS` default to 3600 seconds; accept finite nonnegative overrides, with zero disabling completed-result reuse. Measure age from successful validation using monotonic time. At expiry, coalesce refresh callers without serving expired metadata on failure.
- [x] Apply this cache policy through the shared owner in local operation and integration/E2E setup consumers. Pending concurrent calls share one retrieval; cancelling a waiter must not cancel that retrieval or replay authentication. Cleanup still owns pending work.
- [x] Prove repeated failure then recovery through the joined application HTTP model endpoint, successful reuse, exact expiry/completion-time behavior, zero TTL, malformed result rejection, concurrent refresh and cancellation/cleanup. Preserve unchanged sampled model/settings/requests through independently reviewed source equivalence before carrying forward existing workflow observations; otherwise retain the fresh-qualification gate.

- [x] Keep completed-result cache authority in the shared owner. The worker IPC registry only coalesces pending discovery and delegates later calls; it must not hide expiry or retain a failed result. Bind this transport source in sampling fingerprints and independently verify unchanged qualified requests before carrying forward observations.

## Reviewed sampling source continuity

Independently reviewed offline comparisons preserve all 31 requests across 12
permanent-fixture route/retry/format scenarios through the worker registry and
shared owner for GPT-6 Luna subscription Responses, medium cache and low
reasoning. The 20 retained observations, minimum eight plus four margin and cap
12 are unchanged; no new pilot ran. Two existing source hashes change, while
the registry source is added to the guard, giving 44 bindings. All other source
hashes and sampling fields remain unchanged. The previous packaged wrapper at
`1672559bc0b7bc70d38471298a3709c15970c51d` has SHA-256
`997fc75abe97b80720073883a885ee5a4afdd76ae271491388818b8eaa8255f2`.
The reviewed current wrapper has SHA-256
`a96849852bd5cf0711deb70af1d3fb82039d9b1ca373e5d547874de49713fa17`.
Unrelated source, registry or settings drift still rejects before execution.
This establishes source continuity for the bound profile, not new reliability
observations or broader provider qualification.

## Follow-up human findings

- [x] Close [selected log copying and action-driven model readiness](implementation_plan_h1a3.md) before renewing this checkpoint. Prior verification remains historical; the reported findings are repaired, and human acceptance is recorded below.

## Human acceptance

The user approved Run local and its delivered follow-ups on October 23, 2026.
Broader tracing, product integration and full E2E retain their separate gates.

- [Selected log copying and action-driven readiness](implementation_plan_h1a3.md)
