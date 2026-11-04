# Authenticated remote inference trace ownership

[Execution tracing requirements](implementation_plan_k1a1.md)

## Requirements

- [ ] The worker owns graph execution and the authoritative logical metric trigger.
  The setup host may own the logical generation and physical attempt spans only
  under an explicitly supplied worker SDK parent and compatible export pipeline.
- [x] The parent contract permits only version 1, nonzero lowercase hexadecimal
  trace/span identifiers and integer trace flags 0 or 1. Ambient contexts, baggage,
  credentials, arbitrary attributes and provider payloads cannot cross this seam.
- [x] A fresh challenge and authenticated request bind each ownership offer to
  the complete request. The signed reply binds its exact offer, call sequence,
  request digest and current worker/account binding. Unknown fields, replay,
  mismatched bindings, altered parents and duplicate receipts fail closed.
- [ ] The host grants ownership only after its SDK generation span exists.
  Its one-shot inactive response precedes inference and permits local tracing.
  Missing, invalid or lost acknowledgements leave ownership unknown; they cannot
  justify a second local generation. A failed grant write cannot become inactive.
- [x] Existing independent destination privacy projections remain authoritative.
  This handoff adds no execution journal or additional payload recorder. Remote
  tracing never triggers the authoritative worker metric recording path.
- [x] A disabled worker sends no trace offer or ownership IPC. An incompatible
  host declines before creating its generation span.

## Verification

Synthetic tests cover strict context admission, signed receipt verification,
replay across calls/accounts/requests/parents, request MAC authentication, duplicate
and dropped replies, and failures before and after grant. A spawned worker and
real host send serialized OTLP to a loopback collector: exactly one worker node,
one child host generation and one grandchild physical attempt share a trace.
Known physical usage remains in the worker's existing authenticated ledger.

The worker adapter's suppression and authoritative metric tests are maintained
with the execution tracing runtime. Exporter compatibility uses the captured
runtime pipeline identity; raw settings or exporter credentials are never trace
attributes. Independent review and broad integration remain explicit gates.

- [ ] Independent adversarial review of the frozen implementation.
- [ ] Merged worker/runtime/provider integration and complete required checks.
- [ ] Human acceptance of remote trace presentation and failure diagnostics.
