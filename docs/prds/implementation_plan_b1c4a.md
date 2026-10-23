# Local workflow diagnostic content and durable output

[Live diagnostics](implementation_plan_b1c4.md) · [Local operation](implementation_plan_h1a1.md)

- [ ] Extend Run local diagnostics with graph/component/node identity, activity and outcomes, including tool calls. Reuse admitted workflow observations and identities rather than independently inferring execution from UI-process output. Keep bounded status available without content capture.
- [ ] With LOG_CONTENT=False, show that input/output content is withheld. With True, permit credential-scrubbed user-visible node and tool inputs/outputs. Preserve the exclusion of authentication state, hidden reasoning and registered credentials. Apply privacy checks to each complete admitted payload before truncation, serialization or emission; arbitrary child stdout remains behind its separate streaming-privacy gate.
- [x] Default to explicitly truncated input/output previews. Add a visible, default-false configuration control for full permitted diagnostic payloads, independently of LOG_CONTENT. Full output must not override content/privacy policy or expand model context by default. Document limits and omission markers, including payloads outside supported schemas.
- [x] Keep terminal streaming and the TUI's bounded in-memory display separate. Full permitted output can be retained through a documented durable-output workflow while the console discards older display rows. Define output destination, backpressure, interruption and failure behavior before implementation; do not claim a nonexistent launcher option.
- [x] Use the existing `--no-std-in` terminal mode with stdout piped through `tee` for durable output; do not introduce `--no-std-out`. Share admitted workflow identities and credential redaction across console, journal and tracing, with destination-specific content gates under [K1b](implementation_plan_k1b.md). Reuse the existing OTLP transport for OTEL and LangFuse rather than reading events back from either dashboard. The bounded manual dashboard/login and per-node export join is technically verified; a configured exporter alone does not establish broader instrumentation or deployed conformance. Channel-specific preview limits may differ, but must be disclosed and must not change execution identity or accounting.
- [ ] Verify content-off, preview and full-output states with large synthetic nested graphs and tool calls, split/encoded credentials and control characters. Prove displayed truncation cannot expose secret prefixes, slow consumers cannot silently defeat cancellation/cleanup, and event identity matches the shared live/recorded evidence contract. Renew applicable coverage, causal mutations, installed and boundary checks, independent review and human acceptance.

The bounded manual projection and terminal delivery follow the accepted core
Run local checkpoint. Their durable command is `--no-std-in` with `tee`. Broader
tool schemas, catalog cache-hit/expiry events and inspector channels remain open.

## Non-content readiness and request events

- [ ] Include selected-provider setup/authentication needed, started, ready or failed; catalog acquisition/cache reuse/expiry/failure; and request accepted, executing, completed, cancelled or failed in the local agent log projection. These bounded lifecycle messages remain available with LOG_CONTENT=False, correlate with shared request/component identity where present, and never contain credentials, session state, account descriptors or raw provider errors. Use fixed safe categories and outcomes rather than arbitrary process text.
- [ ] Keep the projection tied to the shared runtime/tracing event source. Verify useful progress during authentication/catalog waits and failures as well as successful requests; prevent duplicate console rows from nested exporters and preserve bounded elapsed/error reporting. No rich lifecycle logging is claimed by the current readiness-only agent source.


## Separate trace-content opt-in

- [x] Coordinate the [independent tracing content policy](implementation_plan_k1b.md) with console/journal fan-out. Console/diagnostic content still requires LOG_CONTENT; enabling trace payloads alone must not expose them in terminal output, JSON/SSE diagnostics or Allure. Both tracing backends use the same trace gate, including their callback/masking paths.

## Current bounded projection

The local renderer consumes the source's immutable, LOG_CONTENT-admitted
projection. JSON lines preserve source execution/event identities and escape
terminal control characters. Default previews retain at most 512 characters per
admitted string; RUN_LOG_FULL_CONTENT=True removes this presentation limit only.
Source omissions and truncation remain explicit and cannot be recovered by the
setting. Unknown nested values are marked unsupported rather than inspected.
Local provider status uses fixed setup/authentication/catalog/request categories
and outcomes with an opaque operation identity. Catalog cache-hit/expiry and
broader tool schemas remain unchecked until observed at their actual owner.

Terminal stdout and stderr use bounded best-effort descriptor delivery (256
outstanding events and 1 MiB, including an active payload). A slow `tee` consumer
causes diagnostic drops while the agent continues; an explicit gap line precedes
resumed payload delivery. Partial writes finish before that marker. Shutdown
allows 0.2 seconds to drain, then cancels delivery and restores descriptor flags;
undelivered output is not guaranteed after interruption or a permanently blocked
consumer. Broken output descriptors disable that destination without changing
agent execution. Pre-close restoration gets one bounded retry; unresolved duties
retain a descriptor-only receipt usable from a later loop. Task allocation and
concurrent retries preserve ownership and the primary error. `--no-std-in` with `tee` retains stdout; `2>&1` before the pipe
also retains safe status on stderr. The TUI's display retention remains separate.

## Bounded manual checkpoint

- [x] Project declared plain node I/O and admitted lifecycle/route/attempt/timeout/accounting metadata into literal JSON lines, with preview/full admission limits and independently bounded TUI retention.
- [x] Independently verify terminal partial writes, shared descriptors, cancellation, explicit gap delivery and observer failure isolation.
- [x] Close joined coverage, installed/package and external review before the new human checkpoint. Broader tool schemas, catalog cache-hit/expiry events and the HTTP/SSE inspector remain separate.
- [ ] Human acceptance of this bounded output behavior.

## Descriptor cleanup acknowledgement

- [x] Attempt every owned descriptor restoration even if another descriptor becomes invalid. Report unresolved cleanup without abandoning healthy descriptors; retries must settle remaining duties rather than replay a cached failure. Verify partial descriptor loss, concurrent/cancelled closers and finite draining.

- [x] Track flag restoration and unattempted close duties independently. After any close attempt, retire the numeric descriptor even when an error is reported; never retry an ambiguous close against a possibly reused number. Continue all healthy duties and preserve close diagnostics. Constructor rollback preserves the acquisition failure; retain retry ownership only for restoration failures before close is attempted. Verify immediate descriptor reuse after close-then-error. The [Linux close contract](https://man7.org/linux/man-pages/man2/close.2.html) supersedes the earlier close-retry assumption.

## Launcher cleanup handoff

- [x] Before the terminal wrapper discards its event loop, make a bounded same-loop retry of remaining pre-close descriptor restoration duties. Preserve the original operation or cleanup error; if restoration still cannot finish, retain an actionable descriptor-only cleanup receipt that works from a later loop, without touching completed close numbers or awaiting old delivery tasks. Verify normal, failed and cancelled operations, transient/persistent restoration failures and final borrowed flags/duplicate closure through the public wrapper.

## Allocation and retry ownership

- [x] Include terminal task creation in resource-acquisition rollback and close unscheduled coroutines. If scheduling cleanup fails, quiesce existing delivery tasks and attempt descriptor release without requiring a new task; preserve the primary operation/error and retain unresolved restoration receipts. Serialize descriptor snapshot, restoration, close and retirement across later-loop retry callers so concurrent threads cannot touch a number already released and reused. Verify all task-allocation boundaries, repeated cancellation and real-pipe reuse. Standard eager task execution must not leave completed destination workers cached or strand later output bursts.

## Direct finalization cancellation

- [x] Every terminal finalization entry point must settle its existing close, manager and worker tasks before descriptor restoration or release, including direct and repeated cancellation of `finish()`. Preserve the original cancellation/cleanup error and any unresolved pre-close receipt. Verify blocked and healthy real-pipe delivery; a wrapper shield alone is insufficient. Direct descriptor-only retry must reject while any manager, close or registered worker task remains active, without awaiting an old event loop or touching descriptors.

## Log-source menu follow-through

- [x] The `*` shortcut must select All logs both from a viewed source and from the dedicated log-source menu; keep keyboard focus and button behavior consistent. Clarify that current UI logs expose child-process activity, while Settings/send requests produce agent catalog/request diagnostics; raw UI-process content remains behind its separate gate.

- [x] If available sources change while All logs is focused in the source menu, rebuilding the menu must preserve that semantic choice; Enter must still open All logs. Verify source additions/removals, including an empty source list, while preserving ordinary source and browser focus behavior.
