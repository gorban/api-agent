# Bounded LLM evidence propagation

[Complete flow-aligned evidence](implementation_plan_b1c1a2.md)

- [x] Preserve permitted actual LLM request and response occurrences under the correct graph, component, node and call for every parsed flow. With explicit LOG_CONTENT=True, retain admitted complete content rather than dropping ordinary conversations due to small per-container limits. Keep one canonical occurrence and reuse it across projections; preserve unattached calls, partial results, retries, errors and truthful omission states.
- [x] Support bounded larger evidence in logical chunks of up to 200 KiB. Scrub the complete permitted content in its credential-owning authority before any IPC, fragmentation, journal or storage; enforce bounded aggregate memory, transport and storage, integrity, ordering and cleanup. Missing, invalid, excessive or interrupted payloads must remain explicitly incomplete, never falsely complete. Keep actual wire-frame limits, credential protection and TRACELOOP_TRACE_CONTENT independent of LOG_CONTENT.
- [x] Preserve manifest-bound canonical resolution and legacy reopen/validation. Verify multiple graph visits, long messages, response and failure paths, concurrent calls, chunk boundaries, secret and hidden-reasoning exclusions, both content flags, corruption and resource bounds through actual provider ownership, IPC and finalized Allure without paid inference. Include meaningful causal controls and independent/external review.
- [x] Custom provider evidence must use an admitted operation credential authority. A legacy exporter without one must produce a content-free withheld indication; restoring its content requires the provider to register credentials and join that authority. A boundary supplied by the provider alone does not establish that it is the operation's credential owner.
- [x] Admit at most one authenticated caller-authority request per completion, including after release. Reject a replay before transferring credentials; require the registered local handoff entry and decline if it is missing. Bind the server's authority gate to the same local registration before and after transfer, rejecting a changed registration and releasing an admitted ticket. Retain any previously acquired lease until authenticated release or confirmed EOF. A later completion has its own admission.
- [x] Finalize evidence in the background without delaying the provider response; workflow and test finalization must wait for owned evidence or explicitly report loss. Concurrent credential registration waits asynchronously for its evidence handoff. On macOS, use a best-effort memory watchdog with bounded input and time, and report its failure without exposing content.
- [x] Human accepts Input, Output, LLM request and LLM response populated within GRAPH hierarchies. Native macOS verification below remains separate.

## Graph capture authority

- [x] Admit graph inputs and outputs through the complete operation credential
  authority before retention or ordinary IPC. Reuse authenticated local authority
  leases for credential-only enrollment: a private permission-restricted Unix
  socket, kernel peer identity, operation/worker binding, fresh mutual challenges
  and purpose-separated authentication protect this explicit trust boundary.
  Credential enrollment is confined to this channel; raw node values remain in
  their producing process. A credential snapshot grants no content permission.
- [x] Bind shared capture services at invocation time with original explicit
  configuration provenance. Explicit workflow authority bundles take precedence
  as a whole; missing authority or policy stays fail-closed. Preserve independent
  logging and tracing permissions and cover workflows created before binding.
- [x] Freeze the complete declared producer inventory and operation authority
  during scrubbed journal admission. Credential registration waits asynchronously;
  release leases before arbitrary subscribers, sinks or traces run. Keep reverse
  enrollment independent of inference locks, use consistent lease ordering, and
  release on cancellation, authenticated completion or confirmed EOF. Verify
  concurrent capture, reentrant callbacks, stale/replayed bindings, lazy provider
  initialization, both flags and truthful incomplete evidence without inference.

- [x] Preserve admitted valid empty request and response carriers as complete canonical payloads. Malformed or unsupported carriers remain explicitly unavailable; they must never claim complete content.

## Bounded local endpoint path

- [x] Keep the authenticated operation-owned credential channel usable when the configured temporary directory would make its Unix socket path exceed the portable descriptor bound. Preserve private socket ownership, peer authentication, operation binding, fail-closed admission and cleanup. Use a short trusted system temporary location only for the control socket; leave bulk temporary data in its configured location. Verify normal and long paths, unsafe roots, failure and cancellation.

## Remaining acceptance

- [ ] Verify the best-effort memory watchdog on native macOS. Deterministic platform controls do not establish native macOS execution.
