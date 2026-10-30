# Automatic source frontend preparation

[Workflow resources and viewer](implementation_plan_b1a.md)

Source-checkout launches should prepare their locked browser dependencies when a
selected operation needs them. Developers still install Node.js and npm themselves.

- [x] At the shared Studio, managed-viewer and unit-execution setup boundaries,
  verify the selected library checkout's frontend dependencies and prepared assets.
  Run its root locked `npm ci` only when preparation is needed, then verify the
  resulting assets before continuing. Resolve the library root independently of
  the caller's working directory or resource-pack root; never install into an
  unrelated directory. A reusable unit driver prepares the executing checkout
  only when it is also the selected base test owner; driving another repository
  must not mutate the caller's library checkout.
- [x] Explain preparation progress and give actionable guidance for missing or
  unsupported Node.js, missing npm, installation failure and timeout. Require
  Node.js 24 or newer. Keep subprocess output from exposing credentials.
- [x] Serialize preparation for the same checkout, recheck after obtaining its
  lock, bound waiting and execution, and retain subprocess cleanup on interruption.
  Do not rewrite the lockfile or overwrite unexpected existing asset bytes.
- [x] Ready launches reuse verified dependencies without another install. Help,
  configuration, collect-only and graph execution do not acquire optional frontend
  dependencies. Builds, static checks and pre-commit remain verification-only.
- [x] Installed Python distributions use their packaged browser assets without
  running npm or requiring a neighboring source checkout. Static file opening
  still needs prior preparation because it cannot run the Python launcher.
- [x] Update README and viewer instructions to distinguish automatic launcher
  preparation, optional explicit `npm ci`, offline reuse and manual Node/npm
  installation. Preserve the separate selected-tool dependency setup contract.
- [x] Verify cold/warm preparation, failures, concurrency, interruption, operation
  boundaries and relocated installed resources. Complete exact applicable unit
  coverage, causal regression checks, independent and external review, and public
  boundary/license checks before delivery.
- [x] Retain all locked tool dependency license notices in the clean source tree,
  source archive and wheel, including notices beneath nested package paths that
  otherwise match dependency-directory exclusions. Do not package executable npm
  dependency trees or weaken license-completeness checks.
- [ ] Human acceptance: start a source viewer or Studio with missing frontend
  dependencies, observe preparation and usable assets, then restart without a
  repeat installation. Check missing Node/npm guidance in a controlled environment.

Human cold/warm preparation review is deferred until the next launcher/onboarding upgrade so its retest covers the final entry point. Technical delivery remains complete; no acceptance is inferred.
