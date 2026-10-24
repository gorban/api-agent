# Local listener request deadlines

[Local startup and shutdown](implementation_plan_k2b.md)

## Requirements

- [x] The owned HTTP listener must serve later requests after its startup budget expires. Detach the long-lived service task from acquisition deadlines while retaining progress reporting.
- [x] Each request retains its configured timeout and any explicitly bound request deadline; cancellation and shutdown ownership remain effective.
- [x] Catalog failure retry, provider-owned authentication and configurable successful-catalog caching retain their established behavior.
- [x] Complete causal mutation checks and independent source/regression review.
- [x] Complete applicable exact coverage and public-boundary checks.
- [ ] Human acceptance: after local startup has finished, open Studio Settings and confirm model choices load.

## Implementation evidence

The listener previously created its service task inside the acquisition timing
scope. Connections inherited that scope, clipping later HTTP body acquisition
to an expired deadline before catalog discovery could start. The service task
now starts inside the shared independent timeout scope. HTTP request timing and
explicitly supplied request budgets retain their existing enforcement.

A deterministic regression starts the real listener wrapper inside a short
startup budget, advances its clock after startup, and exercises the real Studio
HTTP/catalog path with an asynchronous body receive. Before the fix it returns
408 without calling the provider; afterward the catalog succeeds. A separately
bound expired request still returns 408 without starting provider setup.

Focused verification passed 155 cases across local inference, recovery, shared
setup and operation lifecycle suites. Removing the listener detachment reproduces
the healthy-request 408 regression. Independent review passed 114 regression,
operation, Studio bridge and timing cases, with no actionable findings. Additional
checks confirmed the progress sink survives, launcher/readiness timing remains
clipped, nested request timing still applies and listener disposal completes.

A native real-Uvicorn HTTPS proof on a disposable loopback port retrieved the
synthetic catalog after startup expiry. Verified CA/hostname checks and the
session guard remained active; a genuinely stalled request still returned 408.
No inference ran, and all proof-owned listeners/providers closed. This does not
replace the explicit human review of a restarted Studio session.

## Deferred human review

Substantive implementation and required independent/external reviews are complete.
The final joined unit gate passes at exact line and branch coverage, with package,
installed and public-boundary checks. Only explicit human acceptance remains
unchecked above; earlier approvals and historical failed evidence are preserved.
