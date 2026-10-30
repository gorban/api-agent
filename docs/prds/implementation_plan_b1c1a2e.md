# Call and tool evidence progressive disclosure

[Complete flow-aligned evidence](implementation_plan_b1c1a2.md)

- [x] Keep graph, component and node parameters concise. Show inclusive totals from unique authoritative accounting calls, preserving unknown and partial attribution. Place each call's frozen model, provider, effort, usage and cost facts beneath that call rather than repeating UUID-prefixed facts on its node or ancestors. Preserve accounting precision and existing canonical payload identities, hashes and compatibility.
- [x] Keep a logical LLM call visible even when its timing is unavailable; do not fabricate a timed interval. Nest physical transport attempts beneath the logical call. Conversation generation and tool relevance verification are LLM calls; an orchestration node is not itself a tool invocation.
- [x] Observe actual verified tool dispatch through the shared tool execution boundary for every admitted named tool. Place distinct **Tool request** and **Tool response** evidence beneath the owning graph, node visit and tool invocation. Keep these separate from **LLM request**, **LLM response** and physical LLM transport attempts. A proposed or rejected tool call must not appear as an executed call.
- [x] Retain one canonical permitted tool request or response occurrence, with stable invocation identity and manifest-bound native attachments. Preserve nested JSON values and explicit empty, withheld, unavailable, partial, failure and cancellation states. Never infer successful execution or complete content from a missing result.
- [x] Admit tool content through the complete operation credential authority before retention, IPC or storage. Explicit LOG_CONTENT=True permits only scrubbed admitted content; tracing permission remains independent. Missing or unproven authority withholds content. Keep structural tool evidence content-free and preserve provider/runtime ownership and cleanup.
- [x] Verify concurrent named tools, repeated visits, orchestration and relevance LLM calls, unknown accounting, exact-once totals, absent timing, both content flags, credential redaction, rejection before dispatch, failure/cancellation and canonical reopen. Use synthetic owners without inference and causal controls for node parameter expansion, mislabeled tool data, privacy bypass and duplicate payload retention. Complete independent and external review.
- [ ] Record human acceptance of readable call details and distinct actual tool request/response placement.

## Additive tool observation contract

Tool observations use issuer-owned UUIDs independent of model tool-call IDs and
LLM accounting call IDs. A `tool_start` and `tool_end` pair share the exact graph
invocation, node visit, attempt and admitted labels. Times must be finite and
ordered. Each input/output capture carries that UUID; duplicate phases, replay,
changed ownership and LLM accounting or transport fields are invalid. An open or
lost lifecycle makes evidence partial. Tool labels require credential privacy
admission; printable owner labels alone do not authorize disclosure.

Canonical tool occurrences have `kind=tool`, graph invocation, event, visit,
attempt, tool UUID and request/response phase. The manifest-bound payload is
stored once and native attachments resolve that occurrence. Existing node and
LLM occurrences and their hashes remain unchanged. Updated readers admit this
additive contract; old software is not claimed to understand new tool events.

Request admission retains the tool start and scrubbed input under the same
complete credential lease, before releasing public callbacks. Recheck the
verified digest and grant after admission awaits; dispatch a private JSON
argument snapshot while preserving the selected ToolSpec identity. Preserve
legal local schema references when composing request/response envelopes.
Cancellation during response admission remains cancellation, with truthful loss
and terminal evidence rather than a fabricated successful result.

Tool response content includes the actual result status, data, JSON error list
and optional diagnostic, including partial and failure explanations. Apply the
same credential admission and bounded JSON policy to every field; never serialize
exception objects or invent omitted explanations. A selected local emissions
boundary supplies credential projection even when the explicitly supplied journal
uses another boundary. Hold its registration lease through retention, release
before callbacks, and preserve independent journal and trace permissions.
