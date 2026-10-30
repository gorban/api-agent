# Declarative node tool capabilities and diagram references

[Workflow-selected tools](implementation_plan_o4.md) · [Runtime ownership](implementation_plan_o4e.md) · [Viewer](implementation_plan_b1a3.md)

The workflow declaration must determine the tool instance and permitted operations
available to a node. Native functions choose when to call a declared capability;
they must not repeat instance selection or inject a shared tool gate manually.
The diagram and runtime consume the same captured declarations.

## Authoring and capture

- [x] Extend the existing Tools table with an optional Operations column, retaining
  compatible Port/Instance declarations. Node uses link the named instance and
  explicitly list permitted existing ToolGate operations: describe, prepare,
  verify and execute. Reject invalid, duplicate or empty operation declarations
  before effects; preserve lexical scope, replacements and captured provenance.
- [x] Distinguish graph/component availability declarations from node consumers.
  Repeated references to one qualified instance retain every use site and share
  its existing operation owner; equal URLs do not merge different instances.
  Capture both supported parsed-flow formats without importing native code.

## Runtime

- [x] Provide node-local context.tools[port] capabilities from the captured plan
  automatically in both parsed-flow executors. Reject undeclared ports and
  forbidden operations before dispatch. Keep existing configuration precedence,
  factory admission, prerequisite planning and Runtime ownership authoritative.
- [x] Reuse existing ToolGate validation and content-bound invocation grants.
  Planning/preparation, verification and execution may occur in different nodes
  using the same instance within one graph invocation. Preserve grant transfer
  across those declared consumers while rejecting replay, different instances and
  different/concurrent invocations. Fresh calls to the same instance within one
  turn, including repeated aliases and multiple pending calls, remain supported
  with distinct single-use grants and one shared operation owner/borrowed view.
  Call-scoped gate state must not duplicate owner setup, invalidate another
  pending call or leak invocation state into another turn.
- [x] Borrowed capabilities cannot dispose the parent owner. Retain bounded
  setup/operation deadlines, cancellation, failure evidence and retryable cleanup;
  release invocation views with existing lifecycle helpers on every exit.
- [x] Preserve node/port/instance attribution in existing tool request/response and
  GRAPH evidence. Do not add a parallel reporting or ownership framework.

## Examples and presentation

- [x] Migrate the included API demo and applicable named-tool fixtures to explicit
  consuming-node declarations and context.tools. Remove hardcoded application
  port selection and shared tool_gate service injection; keep supported legacy
  entry points compatible through generic boundaries where needed.
- [x] Render unannotated, non-control-flow dependencies from consuming nodes to
  their clickable tool instance. Keep operation details in the node Tools table. Parent diagrams summarize descendant
  consumption without implying that a conditional call happened. Drill-down
  diagrams and declaration links preserve exact scope/port identity.
- [x] Update authoring/provider documentation with a complete declaration-to-native
  call example; explain availability versus consumption and owner sharing.

## Verification and acceptance

- [x] Verify both parsers/executors, inherited scopes, multiple ports/instances,
  conditional consumers, no-import discovery, malformed operations, forbidden
  dispatch, cross-node grants, concurrent-turn isolation, failure/cancellation and
  last-use cleanup. Include causal controls for authorization and diagram/runtime
  agreement; preserve exact usage/accounting evidence.
- [x] Verify real rendered navigation, recursive saved views, source and relocated
  package resources, applicable full unit tests at 100% line/branch coverage,
  affected integrations, scoped public-boundary/license checks, independent
  adversarial review and external PR-style review before requesting human acceptance.
  Combined-candidate publication/history checks remain a separate gate.
- [ ] Human review: follow primary from declaration to consuming nodes in the
  static/managed demo diagrams, run the API demo and named-tool integrations,
  and inspect matching node/port/instance tool evidence in Allure.
