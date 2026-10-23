# Run local selection copy and model readiness

[Previous repairs](implementation_plan_h1a2.md) · [Core operation](implementation_plan_h1a1.md)

## Selected log text

- [ ] Ctrl+C on visible selected Run local log text uses the shared system/terminal clipboard helper, with success/failure feedback. It must not stop the operation or copy an unrelated menu choice. Preserve the existing focused-menu URL/source-name behavior and make no clipboard writes on selection, navigation or refresh alone.
- [ ] Preserve meaningful text selection/focus through unchanged status/log refreshes, and retain the live operation after copy failure. Verify actual Textual key dispatch, selected text, native/fallback copy and errors; a change only to the internal text-widget clipboard is insufficient.

## Model acquisition from user actions

- [ ] Remove the dedicated Retry model list control. If the usable catalog is absent, Send (including the keyboard shortcut) or opening Settings starts or joins bounded catalog acquisition through the existing agent API and selected authenticated provider owner. Existing successful catalog reuse remains governed by the configured status/TTL policy. Never invent model choices, duplicate authentication, or run an automatic retry loop.
- [ ] Show loading while acquisition is pending. A nonempty Send intent waits for readiness and dispatches its message once; repeated triggers coalesce. Empty-message validation does not initiate authentication. Preserve drafts, explicit selection and conversation state on failure; do not submit a stale message after New chat or a changed/cancelled intent.
- [ ] Report acquisition/authentication/connection failures from either action, and non-deliberate failed Send responses/streams, in an accessible modal with a dismiss control and restored focus. Retain safe diagnostic context, avoid raw provider/credential details and HTML interpretation, and allow the next Settings/Send action to try again. Deliberate Stop/cancel is not a failure modal. Preserve existing response/error records and lifecycle controls.
- [ ] Reproduce reported real-provider model selection failure when available; distinguish configuration/authentication/catalog/transport failures from frontend state failures. Use joined real local HTTP/browser checks with controlled provider failures and recovery for deterministic regression coverage. Do not claim real-provider repair based solely on browser mocks.

## Verification and acceptance

- [ ] Close independent adversarial and joined external review, applicable exact line/branch coverage, causal controls, installed/browser/package/notices and public-boundary gates. Preserve unrelated work and established authentication/account ownership.
- [ ] Human review: copy selected log text; open Settings with unavailable models; dismiss its error; retry through Settings or Send; select model/version/reasoning and send successfully. No human acceptance is inferred.

## Current diagnostic scope

The core agent source currently emits its readiness message, not a per-request
node/tool stream. Keep richer execution events and credential-safe content
previews in their owning tracing/live-diagnostics requirements; do not add raw
process output merely to populate this view.

## Empty acquisition is not a reusable success

- [ ] Opening Settings is immediate and never depends on a catalog succeeding; non-model settings remain usable after dismissal of an acquisition error. Treat an HTTP-success response with zero models as a failed model-readiness attempt, preserving the next action's ability to retrieve again.
- [ ] Do not retain an empty completed provider catalog in the shared catalog cache, even when the lower-level metadata wire contract accepts an empty tuple. Preserve the wire contract and pending-call coalescing; the application can report unusable choices, while subsequent retrieval reuses authentication and calls the provider again. Clear orphan empty results too. Prove actual HTTP recovery and strict unchanged-profile source equivalence for any sampling-bound cache repair.

## Reviewed source continuity

The empty-result cache repair preserves the retained GPT-6 Luna subscription
Responses profile with medium cache and low reasoning. Independently compared
12 permanent-graph scenarios preserve all 31 request wires, routes, retries and
outcomes. The 20 prior observations, minimum eight plus four margin and cap12
remain unchanged; this is source continuity, not a new pilot. Of44 source
bindings, only the shared catalog owner's hash changes; unrelated source and
settings drift still reject. The previous wrapper at `f62cc7c` has SHA-256
`a96849852bd5cf0711deb70af1d3fb82039d9b1ca373e5d547874de49713fa17`;
the reviewed replacement has SHA-256
`9e55ac91eaf31061592b980149bd23d84a7478f992c854aff858633e2b249f52`.
