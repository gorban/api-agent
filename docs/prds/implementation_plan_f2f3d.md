# Inference evidence and opt-in reasoning

[Reasoning diagnostics](implementation_plan_f2f3a.md)

## Requirements

This revision covers retained provider payloads (including buffered SDK reasoning), direct workflow tracing, logs and Allure. [Remote tracing](implementation_plan_k1b2.md) extends the shared-provider path; neither revision recovers reasoning discarded by a normalized-only streaming adapter.

- [x] Capture provider-returned plaintext reasoning and summaries separately from the final answer under explicit `LOG_CONTENT=True` (logs/Allure) or `TRACELOOP_TRACE_CONTENT=True` (traces), within the delivery scope above. Destination permissions are independent; DEBUG is not a content permission. Never infer or synthesize reasoning from token counts.
- [x] Continue excluding credentials, encrypted reasoning, redacted thinking and continuation/checkpoint state regardless of content flags. Apply the same credential authority, bounds and redaction to reasoning as other generated text, including serialized/nested carriers.
- [x] Explain omissions at their destination: name its content flag and require a new run when capture is disabled; distinguish absent provider text, capture loss/truncation and always-excluded data. Do not promise historical recovery or that flags reveal encrypted/internal state.
- [x] Keep response reasoning concise and consistent: always use nullable `reasoning_text_characters` (`null` unknown, `0` observed empty, positive count observed text), not a text field present only when absent. Do not inject response-only fields into request diagnostics. Preserve `reasoning_items` as a count and locate actual plaintext in the permitted response payload. Keep actionable disabled-capture, credential-boundary and capture-loss notices; generated text stays outside the metadata-only diagnostic contract.
- [x] Remove routine successful-capture narration. Keep application notices only as explicit capture/omission metadata or separately labelled diagnostic attachments, never substituted into provider text, reasoning or custom attachment values. Keep credential redaction markers and failure/omission status truthful and distinct from provider data.
- [x] Retain bounded content-free diagnostics in Allure even with DEBUG and content capture disabled: SDK-observed output shape, visible/reasoning character counts, completion/stop/incomplete categories, effective controls, safe response/request identifiers, HTTP status and SDK versions when available. Correlate with existing logical-call and physical-attempt identities; label SDK observations, not raw-wire evidence. Unknown data stays unknown.
- [x] Preserve response content attachments, accounting, failure evidence and transport attempts in standard Allure results, with distinct diagnostic attachments. Do not expose arbitrary headers, exception bodies or auth transcripts.
- [x] Keep empty visible answers invalid; reasoning must not become an answer. Preserve all original calls, scoring and retry behavior. No paid requests are needed for this evidence change.

- [x] Complete the independent review and scoped verification of [remote trace-only policy/owner propagation](implementation_plan_k1b2.md). Its backend-owned collector is separate from the log lane. Buffered provider reasoning fields survive schema projection consistently into journals, live log callbacks and trace events. Normalized-only streaming capture cannot supply discarded original reasoning; document it as unavailable, not absent.

## Verification and acceptance

- [x] Surface HTTP-success response-validation failures with fixed, bounded contract diagnostics and retained usage, independent of DEBUG. Preserve fail-closed credential guards and do not retry malformed successful responses or silently change LLM/tool behavior. An unavailable response attachment must be distinguished from no upstream response.
- [x] Retain bounded SDK response evidence after contract rejection when independent credential-safety checks succeed. Apply the existing independent log/Allure and trace content permissions, complete credential union, encrypted/state exclusions and capture limits. Safety rejection must withhold the body even with both flags enabled. Preserve the failure, usage and fixed validation categories; identify known validation rules and schema paths without arbitrary field names or values in metadata. Evidence capture must not change execution or retry behavior.

- [x] Exercise independent destination flags, default omission guidance, plaintext reasoning, secrets embedded in reasoning, encrypted/state exclusion, safe SDK diagnostics and Allure correlation through shared setup transport.
- [x] Verify causal privacy/empty-answer regressions, scoped unit tests and exact new-module line/branch coverage; complete independent review and scoped public-boundary checks.
- [ ] Renew the complete joined coverage, installed-artifact and intended-index/publication gates before committing or publishing. Scoped verification is not a fresh successful whole-project delivery gate.
- [ ] Human review of new Allure evidence remains separate from resolving upstream empty answers or qualifying reasoning-test stability.

## Evidence

The joined focused suite passed 1,938 cases; subsequent safety checks passed 1,064, source-worker/recorded-consumer checks passed 364, and terminal/capture checks passed 168. The new diagnostic module has exact 100% line/branch coverage. A broader unit sweep exposed import-alias harness failures, outdated frame/attachment expectations and a missing completed-response withholding marker; consistent-path reruns and the marker repair cleared the reported failures. This is not a new whole-project coverage qualification. Process-local mutations of content gating and encrypted-state exclusion made their rejection tests fail; no mutation remains. Empty-answer validity is also checked against real adapter decoding.

External PR-style review closed optional SDK-metadata failure and misleading capture-notice findings, then independently checked the withholding-marker repair. No live calls, credential changes or test-acceptance weakening were required.

The earlier concise-null follow-up passed 122 diagnostic tests with exact 100% line/branch
coverage (142 statements, 92 branches), plus 126 related content/recorded-Allure
checks. Reintroducing the routine absence paragraph in a process-local mutation
failed all six request/response null regressions; no mutation remains. Scoped
public-boundary checks passed. Diagnostic IPC stays metadata-only and strict;
nullable presentation fields are added only when rendering the attachment.

The consistent-field revision supersedes the absence-only text placeholder with a
nullable response character count and removes that count from requests. Its 129
diagnostic tests pass with exact 100% line/branch coverage (142 statements, 92
branches); 815 joined provider, worker, capture and recorded-Allure tests pass,
plus 491 related capture, trace and adapter checks. External PR-style re-review
independently passed the 815-test suite and closed with no actionable findings.
Focused process-local mutations of the request-field exclusion and tool-finish
validation made their regression tests fail; no mutation remains.

A reported HTTP-200 tool-conversation rejection did not retain its response body,
so its exact cause remains unknown. The selected live integration test passed on
rerun; this is not proof the intermittent problem is resolved. That initial
revision retained fixed codes, admitted metadata and reported usage, but marked
both contract- and safety-rejected bodies unavailable. The subsequent opt-in
failure-evidence revision distinguishes them: independently guarded contract
evidence follows the destination flags; safety-rejected or unretained bodies
remain unavailable. No flags recover earlier content, and no successful-response
validation or retry policy changed.

The opt-in rejected-body revision passed 1,050 joined provider, SDK, capture,
projection and recorded-Allure/trace tests. A 440-case coverage run measured exact
100% line/branch coverage of the shared rejected-evidence guard (82 statements,
54 branches) and diagnostics (142 statements, 92 branches). All four destination
flag combinations were exercised through actual SDK/setup subprocesses and a
local trace receiver, with plaintext reasoning included only at permitted
destinations and encrypted/state fields excluded. Shared rejection admission is
repeated against the final frozen credential snapshot; its internal marker is
independent of scrubbed diagnostics and never crosses evidence IPC. Regression
cases cover reasoning aliases, keys, multi-field fragments, escapes, embedded
JSON, late registration, malformed markers and bounded work. Process-local
mutations of the final-snapshot and multi-fragment guards made their dedicated
regressions fail; no mutation remains. No additional live calls or credential
changes were needed. Full joined publication gates remain separate.
Independent PR-style re-review closed the split-carrier, escape and final-snapshot
findings, independently passed 517 focused cases and reported no remaining
actionable issue in this scope. Human inspection of a new failed-run report and
the original intermittent provider problem remain open.
Another 180 transport/framing and guard checks passed. Scoped source scans and whitespace checks passed; these
are not a complete intended-index or publication qualification.

The capture-notice audit removed routine successful-reasoning narration and
replaced custom attachment placeholder content with separately labelled capture
diagnostics. The request/response, log, trace and Allure paths audited retain
producer values separately from application notices; redaction markers remain
intentional privacy transformations. Verification passed 507 joined tests and
193 focused coverage cases, with exact 100% line/branch coverage of diagnostics
(136 statements, 88 branches) and scoped evidence (148 statements, 84 branches).
Independent review passed 214 cases without remaining findings. Process-local
mutations restoring the routine notice or fake content value failed their
regressions; no mutation remains.
