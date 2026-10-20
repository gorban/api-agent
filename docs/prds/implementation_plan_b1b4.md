# Stateful workflow execution

This public capability accepts a versioned, fully declared graph. Its root links ordered node and component declarations. Each node names an admitted native callable. The root names one TypedDict state schema, registered reducers, and every static or conditional target. A router observes state after the source node update and returns exactly one declared target or END. An approval or clarification result ends the current turn; the next turn starts at the graph entry with retained state.

## Requirements

- [x] Keep Workflow v2 parsing and execution behavior intact. Workflow v3 must reject undeclared or ambiguous nodes, edges, reducers, source links, and router targets before native execution.
- [x] Admit root, child declarations, schema/node/router sources, and every declared first-party helper through bounded immutable resource capture. Verify freshness before any native import or execution; include resource hashes in the graph identity.
- [x] Declare local Python package imports in a `Native Dependencies` table. Capture their bytes before native import, reserve every declared qualified name at admission, and resolve imports deferred into native function bodies from the captured bytes throughout graph execution without relying on ambient `sys.path`. Reject undeclared local imports, competing owners (including unloaded names), and stale loaded modules; release new reservations and imported modules after a failed catalog load, including imports in flight during rollback. An import must recheck ownership after executing package code and fail if its admission ended. Keep installed third-party dependency identity in the separate package proof.
- [x] Allow imports from the actual loaded public runtime package as trusted host dependencies, based on physical package origin. A selected graph's same-prefix local modules still require explicit dependency admission.
- [x] The optional root `Resources` table declares stable ID, kind and relative source for each prompt, configuration, data, schema or asset resource. A selected replacement may target a resource ID before the original is read. Reject duplicate IDs, unknown targets, kind conflicts and root escapes. Expose selected bytes and hash through node context; nodes consume admitted snapshots rather than reopening files.
- [x] An optional selected workflow library names a source-relative sibling root and an installed distribution/resource fallback. Keep the runtime base for app configuration, tests and providers; use the selected library root as the workflow's logical base for entry, graph capture, replacement origin and native catalog admission. Verify installed resource ownership from distribution metadata without importing package code; reject symlink aliases in source, resource and inventory paths.
- [x] The optional `Timeouts` table binds graph, component and node owners to admitted numeric configuration keys. Enforce finite positive values and each ancestor's remaining deadline. Preserve declared key and effective budget in content-free timing evidence.
- [x] Validate timeout declarations and defaults during admission. Pass the effective selected configuration to `load_stateful_catalog(definition, config=...)` so invalid overrides reject before native imports; pass the same configuration to `StatefulWorkflow` or `load_workflow`, which rejects a mismatch.
- [x] Use a registered TypedDict schema with explicitly registered reducer callables. Support LangGraph's `add_messages` reducer for message append and replacement by ID. Native nodes receive current typed state plus services/request context and return partial updates.
- [x] Compile an idiomatic `StateGraph(schema)` with declared entry and routing. Retain process-local checkpoints by explicit thread ID. A later turn starts at the declared entry using retained state. Durable storage and arbitrary mid-node interrupts are outside this minimum capability.
- [x] Preserve native `astream_events(version='v2')` events, including callbacks from LangChain Runnable model and tool adapters used within native nodes. Emit only content-free scope, node, edge, timing, call, and outcome facts to the journal, with graph START and END evidence. Cancellation and failures drain owned work and close scopes before releasing the conversation lease.
- [x] Verify each logical source or installed owner before native import and each reporting-enabled turn. Bind the journal's generated or caller-supplied turn ID to provider accounting; reject a conflicting explicit turn ID before any graph or native effect. Join terminal model call IDs to their active node visit without exposing state or prompt content.
- [x] Test with independently authored synthetic declarations and native functions: reducer merge, message replacement, state-dependent route, terminal turn followed by normal-entry continuation, undeclared target/source rejection, changed source rejection, event semantics, and cancellation.

## Acceptance

- [x] Unit tests pass with complete line and branch coverage for changed runtime code before commit.
- [x] Applicable public-boundary scans and license/provenance review pass before public acceptance. Human acceptance remains separate.

The generic schema and graph concepts follow the public [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), and [streaming](https://docs.langchain.com/oss/python/langgraph/streaming) contracts.

## Selected library environment

- [x] Admit the selected source workflow library's project dependencies through the launcher without importing native code; preserve required package metadata for ordinary installed delivery. See [dependency admission](implementation_plan_b1b4a.md).

- [Installed delivery and physical discovery aliases](implementation_plan_b1b4b.md).

## Minimum runtime verification

The stateful runtime, source/dependency admission and selected-library delivery passed independent runtime review, controlled transport and source/relocated-wheel checks. The complete applicable public suite passes 12,100 tests with exact line and branch coverage. Actual source delivery is `f5181ab`; sampling fingerprint binding follows in `230d15d`. No viewer or application acceptance is implied.

- [ ] Complete the final external PR review for this delivery.
- [ ] Human acceptance of stateful execution and selected-library behavior.
