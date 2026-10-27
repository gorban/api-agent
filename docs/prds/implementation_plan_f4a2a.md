# Owned credential readiness transactions

[Native session adapters](implementation_plan_f4a2.md) · [Operation profiles](implementation_plan_f4l3.md)

Concurrent ordinary tests may acquire the same selected SDK profile. A valid
same-identity refresh must not appear as malformed credentials merely because
another owned worker publishes between snapshot reads or before publication.
This scope repairs coordination; it does not broaden supported identities,
credential sources or authentication modes.

- [x] Serialize the entire owned readiness transaction with the existing persistent
  cross-process lock: first snapshot/credential read, exact-tuple identity check,
  native refresh if required and verified publication. A waiting acquirer rereads
  the newly published snapshot and reuses valid material without redundant refresh.
- [x] Preserve selected-profile and verified-principal binding, explicit account/
  principal assertions, complete material/expiry validation, deliberate manual-edit
  rejection and compare-before-write checks. Do not accept an external writer's
  changed tuple merely because it claims the same identity or resembles an owned
  snapshot. Preserve unrelated profiles and caches.
- [x] Permit nested publication on the same store/thread without reacquiring its
  kernel lock. Serialize other threads using that object and separate objects or
  processes using the same persistent lock. Reset nesting/ownership state on
  exceptions, close descriptors on failures and keep lockfiles persistent after
  unlock; never unlink an inode that another worker may be using.
- [x] Keep lock acquisition and readiness inside the existing owned worker and
  shared deadline/cancellation boundary. Cancellation or deadline expiry while
  holding or waiting for the lock must terminate owned work and release descriptors;
  a later worker must proceed. Preserve shorter parent deadlines. Never replay
  inference, switch identity or start another acquisition source to hide failure.
- [x] Prove real spawned-process overlap, waiting-reader reuse, same-object thread
  exclusion, nested exception release and held/waiting cancellation/deadline
  cleanup. Retain manual-edit, identity and publication rejection regressions.
  Removing only the outer transaction lock must fail the overlap regression.
- [x] Complete distinct independent high review and external source review, source/
  installed-resource checks and the applicable complete unit gate: 18,674 tests,
  exact coverage of 45,926 statements and 17,992 branches. These results qualify
  the reviewed source scope, not later live or final artifact acceptance.
- [x] Complete fresh matching workflow qualification after the source change and
  pass the entire normal default-driven catalog: 17 core cases plus one provider
  UI case, with clean reporting, full accounting, cleanup and publication.
- [x] Reconcile final packaged resources, source identity, public boundary/licenses
  and final independent/external review after qualification
  and documentation.
- [ ] Human acceptance through the ordinary launcher remains separate, including
  each authentication adapter's retained acceptance requirements.

The earlier failed default-suite run remains a failure. Successful prior pilots
retain their original source fingerprints; neither their observations nor their
source identities are rewritten to qualify this repair. Broader indexed/federated
acquisition, SSO interoperability and platform qualification remain with their
existing owners.
