# Retained Studio ownership and accounting

[Retained Studio requirements](implementation_plan_i2c2.md)

## Contract

The original Agent listener and prepared browser remain owned by their UI
inspection session. The listener starts in an independent lifetime scope;
individual HTTP requests still receive bounded operation contexts. Startup
readiness deadlines cannot become later request deadlines.

A Studio integration bridge admits one automated completion. Admission retires
the test handle before yielding; fixture teardown also retires it when browser
setup or the test fails before that call. Catalog discovery cannot spend
inference. Further calls remain unavailable until the recording owner publishes
successful suite-accounting finalization. Missing, malformed or unavailable
finalization evidence grants no access. Ordinary standalone pytest has no such
authority. Parallel workers share the recording operation's finalization gate.

After finalization, first catalog discovery or a submitted request lazily creates
a distinct provider owner through normal selected-provider configuration,
credential registry and selected account index. It never revives the closed test
client, borrows its lease, changes account, or reuses its retired interaction
transport. Matching persistent authentication may be reused only through the
provider's normal validation. Inability to authenticate remains an explicit
provider failure. Opening a browser alone never submits inference.

The allowed configured catalog, default-model policy, versions and reasoning
choices remain available. The automated prompt still uses the configured default
and asks for `Ok`; it does not narrow all subsequent selections to that model.

Manual calls use an independent in-memory usage ledger, distinct activity IDs,
provider cost quotes and local quota observations. They emit no pytest inference
or quota events, append no original Allure artifacts and update no historical
suite records. Original suite quota measurements finish before manual access.
The suite exclusion lease stays retained until inspection cleanup finishes.
Completed inference retains its terminal outcome, timestamp and reported tokens
exactly once even when optional cost quoting or quote export fails. Ordinary
quote/export failure means unavailable cost; it cannot turn completed inference
into a failed response. Quoting interruptions still propagate after terminal
usage is captured. Failed inference retains any provider-reported usage without
quoting a successful result. No quote/export error text enters accounting.
A queued manual-provider creation rechecks closure under its ownership lock;
closing the session cannot permit a later factory call or reopen the owner.

End-inspection closes HTTP request ownership before disposing the manual
provider and its authentication browsers on their creating loop. Incomplete
cleanup retains the whole owner for retry, including nested receipts and later
duties. Listener and HTTP shutdown must be acknowledged before manual provider
or authentication/browser dependencies are released. Marking the bridge closed
denies new requests without losing the disposal duty.

## Controlled verification

- [x] Real HTTP automation retires its handle, blocks repeated calls before the
  gate and permits a separately owned configured model/reasoning selection after
  finalization. Suite-event bytes remain unchanged; manual tokens are separately
  counted.
- [x] Actual prepared browser submits a later manual turn on its original page
  after the original test handle is made unusable. No replacement browser is
  used as acceptance evidence.
- [x] HTTP requests after startup scope expiry remain bounded and usable.
- [x] Controlled cancellation, provider failure with usage, unavailable token
  metadata and retryable provider disposal retain accounting and cleanup.
- [x] Optional quote exceptions, invalid quotes and failed/invalid quote export
  retain completed outcomes and tokens with unavailable cost. Quote/export
  interruptions preserve terminal usage before propagating; successful quotes
  and failed inference usage remain attributable once. A creation-lock barrier
  verifies that queued work cannot create a provider after session closure.
- [x] Default unit controls independently cover adapter selection, reasoning,
  usage metadata, retirement, provider failure, cancellation and ordered retry
  duties at exact line/branch coverage. Real endpoint controls retry exported
  receipts through authentication failure and pending HTTP shutdown.
- [x] Early setup failure and retirement-hook exceptions still run evidence
  capture and owned disposal.
- [x] Joined serial and two-worker subprocess/IPC controls prove successful
  recording finalization enables manual requests only afterwards; failed
  publication remains unavailable. Original artifacts and test prompt counts
  stay frozen, and end-inspection closes original workers and listeners.
  These controls isolate history/cache state.
- [ ] Independent review, applicable exact coverage and package/public-boundary
  gates cover the joined implementation.
- [ ] Human frontend acceptance covers idle pauses, composer/history, Stop/retry,
  themes, clarifications and metadata with the retained backend.

No live authentication or additional paid inference is part of these controls.
