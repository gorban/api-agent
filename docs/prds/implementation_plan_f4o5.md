# Bedrock SDK worker-group cleanup after early rejection

[Bedrock Runtime](implementation_plan_f4o.md)

An early provider rejection may arrive while the SDK worker and its credential
helpers are exiting. A process-group signal may report permission denied during
cleanup. Cleanup must not replace the provider's typed error or infer group
absence from the leader's exit alone.

- [x] On a permission-denied group signal, allow one bounded leader-reap attempt
  and probe the group. Only a missing group confirms disposal; a live or
  unverifiable group retains the transport owner and failed signal phase for
  retry. Preserve earlier successful signal phases. Never retry a numeric
  group after confirmed absence.
- [x] Keep the original provider rejection or cancellation primary if cleanup
  fails. Attach a visible pending-cleanup receipt and retry owner; without a
  primary error, report cleanup failure. Block all automatic negotiation or
  transport replay and new worker admission until disposal is confirmed. Retain
  credential authority leases until the worker group and lease are disposed.
  Do not silently treat permission denied as successful termination.
- [x] Test the TERM and KILL permission races, confirmed absence, live and
  unknown helpers, cancellation and disposal retry with bounded synthetic
  workers. Verify exact focused line and branch coverage, public boundary and
  independent review before accepting the repair.
- [ ] Human acceptance: retry the affected route on an authorized macOS host
  and inspect the original error classification and worker cleanup evidence.
