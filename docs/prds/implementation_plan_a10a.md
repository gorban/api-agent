# Noninteractive failure diagnostics

[Failure details and result-list return](implementation_plan_a10.md)

## Requirements

- [x] Explicit NO_STD_IN integration execution prints identified per-test failure
  details to stderr before the final summary. Prefer captured rich diagnostics;
  retain ordinary pytest tracebacks and passed outcomes with a run-level cause.
  Each selected test is identified once. Keep status, selectors and exit codes.
- [x] Full available exception messages, normal/suppressed chains, tracebacks,
  command/exit/stdout/stderr and explicit truncation markers remain visible without
  frame locals. LOG_CONTENT=False does not suppress failure diagnostics.
- [x] Scrub credentials only: registered/configured secret values and recognizable
  credential assignments, headers and URL userinfo. Keep paths, node names, code
  locations, noncredential URLs and operational reasons. Protect secrets split
  across streamed chunks. Never enable inference-content attachments implicitly.
- [x] Exceptions outside returned outcomes also print full scrubbed diagnostics.
  Preserve execution acceptance, missing-requirement exit 2, execution failure
  exit 1, inspection failure exit 3 and interruption exit 130. Never read stdin.
- [x] Pending cleanup remains owned/retryable. The CI boundary can raise an explicit
  sanitized exception wrapper from None after rendering the original exception
  type/chain; preserve the same owner and retry-receipt objects. Preserve interruption
  and numeric SystemExit behavior. An automatic host traceback must not leak the
  original exception's credential message or raising-source literal. Install no global exception hook.
- [x] This channel is explicit terminal diagnostics. Safe RunnerEvent fields,
  Allure, telemetry and persisted artifacts retain their existing policy.
  Do not start services or repair failures merely to demonstrate logging.

## Acceptance and test mappings

- [x] [Controlled noninteractive launch proofs](../../tests/unit/test_terminal_failure_diagnostics.py) exercise the actual selected-test
  argument shape, LOG_CONTENT=False, two identified outcomes and complete command
  evidence; configured secrets and chunk-split values stay absent.
- [x] [Scrubber/formatter proofs](../../tests/unit/test_terminal_diagnostics.py) cover registered/configured values, recognizable credential
  syntax and useful noncredential context, without blanket text suppression.
- [x] Exception/cleanup proofs in those tests cover full chains, preserved exit/status and exact
  cleanup objects and retry callbacks; an unhandled subprocess wrapper leaks no raw secret.
- [x] Prior launch, inspection, recording-isolation and interactive regressions
  pass. Sol high review and focused mutations verify retention and scrubbing.
- [x] All applicable units and exact complete line/branch coverage pass; public
  index/metadata audit precedes a scoped local commit.
- [ ] Human reruns the noninteractive selected-test command and verifies CI details
  before underlying integration execution repairs proceed.

## Evidence

Local interactive diagnostics exposed a prerequisite port conflict. The terminal
completion path printed only summaries and omitted the richer result field.
Sol low implementation and Sol high review verify the owned terminal boundary.
Tests retain operational metadata, command paths and role names while scrubbing
known/registered credential values and recognizable credential syntax. Dynamically
retrieved secrets must still be registered or scrubbed by their owning provider.
Grouped exceptions include child evidence beyond default formatter limits.
All 5,890 full-suite cases pass. Two new focused retained-cleanup cases also pass
on unchanged production; appending their coverage completes exact 18,119
statements and 6,840 branches with zero missing lines/branches. The 433-file public
index and intended/committed metadata pass boundary checks with no new exceptions.
Independent review and cause-retention, credential-scrubbing, grouped-evidence and
cleanup-guard mutations pass. Human CI acceptance remains open; no live
integration execution, port/configuration changes or resource repairs occurred.

[Credential and cleanup completeness](implementation_plan_a10b.md) tracks additional regression requirements.
