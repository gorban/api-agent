# Local operation runtime and frontend join

[Application prerequisites](implementation_plan_h1a.md) · [Startup browser](implementation_plan_k2.md) · [Prepared sessions](implementation_plan_i2c.md)

## Shared operation ownership

- [x] Provide one long-lived local operation owner for interactive and terminal consumers. Reuse shared configuration, TLS/listener, provider, prerequisite and cancellation ownership; no production imports of pytest or testing adapters. Resolve active capabilities before provider initialization and do not initialize unrelated adapters.
- [x] Load the actually selected immutable application definition and resolved overrides before startup. Execute the accepted bundled chat contract through that definition; never substitute direct inference or a different displayed graph. Reject unsupported application bindings before effects. Preserve real selected model/version/reasoning, outcomes and usage. Private/product application adapters retain their separate integration gates.
- [x] Reuse lazy selected-account LLM setup and scoped credentials with a persistent operation lifetime and fresh per-request deadlines. No-UI startup, home/status and flow viewing perform no credential reads, model discovery, quota calls, login or inference. Enabled UI preparation retains its single owner and selected-index binding; coalesce an explicitly needed LLM setup without account fallback or duplicate login.
- [x] All local browsers, including later authentication, are headed. Keep test visibility unchanged. RUN_UI enables the optional frontend; RUN_UI or DEBUG opens the startup browser, with no automatic browser when both are false. A browser closing does not prove dependent resources were disposed.
- [x] Consume only enabled declared prerequisites through the accepted manager/session and expose their verified endpoint/status/log data without rewriting developer configuration. Complete listener/request shutdown before releasing dependencies. Pending cleanup retains the owner and blocks a successful menu/exit transition until acknowledged.

## Shared presentation and safe destinations

- [x] Expose bounded immutable status/log events and a safe snapshot to both terminal and TUI consumers. Preserve component identities, named log-source defaults and raw single-source versus labeled combined output. Reject unknown/numeric sources before startup; do not advertise inaccessible remote logs. Status/home pages expose safe health and verified destination links without raw configuration, credentials, session tokens or diagnostic details.
- [x] Reuse one managed browser/context for home first, actual selected flow next and optional provider-prepared UI last. Share the read-only viewer host instead of launching a second viewer operation; retain home/flow URLs without automatic browser startup. Preserve original-session CDP inspection, tab readiness/order and cleanup. Tracing dashboard requirements remain separately governed by K2 and follow this core checkpoint.
- [x] Replace main-menu and explicit terminal Run local placeholders with the same owned operation. Opening status/log controls or changing sources retains the live session. Stop/Exit/interrupt awaits ordered cleanup, preserving retry receipts and meaningful failure exit status.

## Verification and acceptance

- [x] Prove lazy/no-UI startup, selected graph traversal, settings, multi-turn request/cancel/heartbeat behavior and late request deadlines with controlled real HTTP/browser checks. Include selected pack overrides and source/installed relocation; original suite histories are not manual activity records. Use causal mutations for eager authentication, graph bypass/stale selection and skipped cleanup acknowledgement.
- [x] Complete exact applicable unit line/branch coverage, package/notices, public index/history/metadata checks, independent lane reviews and joined external review before delivery. Live authentication/inference checks require their own authorization.
- [ ] Human acceptance of the delivered local-run scope. Earlier infrastructure and retained-test/Studio acceptance remain valid; broader tracing, private/product integration, application E2E and later workflow inspection keep their separate gates.

## Core-first delivery boundary

- [x] Deliver this checkpoint with tracing disabled. A nonempty TRACE_MODE fails with an actionable diagnostic before provider, browser, listener or prerequisite startup. Preserve existing tracing behavior in other modes. Add tracing dashboard configuration, readiness and login in the following batch under K2; do not silently ignore an explicitly enabled tracer.

## UI-process diagnostic policy

- [x] Consume selected UI-process output continuously through an owned bounded reader, but emit only fixed activity messages and byte counts in this checkpoint. Never decode, retain or forward arbitrary child bytes to log consumers, regardless of LOG_CONTENT. Register generated process credentials before launch. Preserve existing scrubbed agent and infrastructure diagnostics. A later content-logging contract must prove credential exclusion across read, line, encoding and truncation boundaries before enabling raw UI-process text.

## Failure diagnostics

- [x] Scrub terminal and TUI failure messages through the operation's retained credential boundary, including setup failures before session handoff and cleanup retries. Register nonempty string session tokens before catalog or completion callbacks. Preserve actionable ordinary causes while escaping terminal controls. If no trusted owner exists, show a fixed safe failure message; do not render arbitrary factory exception text or replace pending cleanup ownership with a successful exit.

## Open human-review follow-up

- [ ] Close the [selected-log and model-readiness findings](implementation_plan_h1a3.md). Earlier verification is preserved; substantive repairs and renewed review precede human acceptance. Tracing/product/full E2E remain separate.
