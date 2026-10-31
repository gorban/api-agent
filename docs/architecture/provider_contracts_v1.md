# Provider contracts, version 1

The executable declarations are in [contracts.py](../../src/api_agent/providers/contracts.py), [LLM data](../../src/api_agent/providers/llm.py) and [operation control](../../src/api_agent/providers/control.py). This document records the implemented foundation. Distribution, declaration authority, worker inspection and credential IPC remain open in [consumer hardening](../prds/implementation_plan_m3.md); human acceptance of the concrete consumer contract is pending. API payloads remain adapter-defined. Core does not parse external authentication tokens.

## Factory and capabilities

Factories receive resolved configuration and runtime services and synchronously return an object with integer `version = 1`. Services carry verified TLS, emission boundaries and the declared path owner. Runtime startup and disposal own awaited lifecycle hooks.

`ProviderCapabilities` declares kind, supported operations, credential roles, an optional LLM role and optional LLM features. Kinds/methods are: API invoke/ainvoke; LLM describe/adescribe, complete/acomplete, stream/astream, usage/aread_usage; access describe/adescribe, usage/aread_usage; UI launch/astart; upstream validate/avalidate. Access adapters cannot advertise completion or streaming. Version-only foundation factories may load without claiming an operation. Only declared capabilities may be executed.

Use `aload_provider` inside an event loop. Synchronous loading rejects active-loop use before importing a factory. Optional lifecycle/authentication hooks must have their declared async shape and signature; route setup permits synchronous or asynchronous hooks. Streaming permits an async generator or a regular factory returning an async iterator. `require("llm", "stream")` checks that return value at invocation; discovery never calls the stream. Other selections return the adapter itself. The managed session stream also accepts an awaited factory; its iterator must provide awaited close.

## Common inference data

Validated JSON response consumers use
`api_agent.providers.rich_inference.response_json(result)`. It preserves the
original result text and uses recorded actual mode: only `prompted_json` permits
closed whole-response backtick envelopes. Native modes and results without mode
evidence stay strict. The shared parser tries JSON first, then unwraps at most
four matching `json`/unlabelled envelopes with strictly decreasing fence widths.
Original schemas, duplicate-key checks and size/depth bounds still apply. Tool
arguments, schemas, credentials and IPC framing do not opt in. See
[the envelope contract](../prds/implementation_plan_f1a1a.md).

`LLMRequest` carries `selected_llm`, a `CompletionRequest` and an optional request ID. Requests contain ordered messages, optional tools, generation options and optional structured-output schema. Message roles are system/user/assistant/tool. Text, tool calls and tool results use separate immutable carriers. Tool arguments remain a transport-neutral string; adapters interpret them. Schema strings must parse as finite JSON objects.

Available selections advertise `tools`, `structured`, `temperature`, `max_output_tokens` and `cache_policy` where supported. Call `AvailableSelection.validate(request)` before invoking the transport; unsupported selection/features raise a safe `OperationalError("unsupported")`. Concrete adapters must not silently discard requested features.

`GenerationOptions.cache_policy` is None (provider defaults), off, medium
(shortest supported retention), or high (longest supported retention). The shared
LLM_CACHE_POLICY setting defaults to empty, normalizes to None, and applies only
when the call has no explicit policy. Providers must reject unsupported
policies rather than silently substitute them. Empty omits upstream cache controls;
it does not assert caching is disabled. Authentication caches and completed-answer
caches are separate. The integration owner checks advertised cache support and
calls optional synchronous `validate_request(request)` before authentication.
That hook is pure and must raise a safe error on unsupported request options.
The backend performs this preflight before readiness, without cancelling another
consumer's already-running authentication.

`LLMResult` holds an assistant message, outcome (completed/length/tool_calls/refused) and usage. Counts may be unknown (`None`), including when a transport supplies no data. Input totals include cached input; output totals include reasoning. These subsets are not added again to totals and cannot exceed their known totals. There is no inferred price or invented zero usage.

For explicit `CompletionRequest.response` policies, adapters resolve the format
before dispatch and construct `ResponseEvidence` for that resolution. After
authentication and request validation, immediately before admitting the transport
call, invoke `record_response_dispatch(evidence)` from
`api_agent.workflows.inference_capture`. Return the same evidence in the result,
or attach it to a postdispatch failure. Shared IPC requires a matching observed
dispatch event; terminal-only mode claims are rejected while explicitly reported
usage is retained. Predispatch failures carry no actual-mode evidence. This hook
records transport admission, not proof of paid execution. Legacy requests keep
their existing behavior. See [response modes](../../src/api_agent/llm_providers/gpt/RESPONSE_MODES.md)
for format selection, fallback and content-control details.

`StreamEvent` is text, tool_call or completed. The terminal completed event carries the result and terminal usage. Clean exhaustion requires exactly one terminal event; duplicate terminal events or output after it are invalid. Early explicit closure may have no terminal result and must not invent one. Text, tool arguments, schemas, result payloads and browser objects are excluded from carrier repr.

## Selection and activation

LLM_PROVIDER, API_PROVIDER, UI_PROVIDER and ACCESS_PROVIDER select file factories. There are no default external factories in public core. An API component may contribute UPSTREAM_API_PROVIDER. Selected nested declarations are discovered without executable imports and use effective-pack defaults plus explicit dotenv/environment/CLI overrides. Selection changes rebuild schema contributions and both configuration inventories. Component overrides marked override_if_present=true require override=true and apply only if the target field exists; this permits optional callback overrides without introducing fields owned by an inactive provider. Present targets still require compatible types.

Configuration discovery and executable activation are separate. `activation_kinds(values, required_kinds=...)` selects the operation's capabilities. API activation includes its upstream dependency. Explicit access-only or LLM-only consumers avoid unrelated factories and API identity loading. Default run-local/integration/E2E startup activates configured API/upstream/LLM/access owners; UI activates only for E2E or local-run with RUN_UI. No mode, help, unit, collection and configuration modes activate no executable providers. Callback-only consumers can request access routes without starting a frontend. This foundation does not implement callback forwarding or the next provider-session operation consumer; explicit UI integration activation must use a declared capability intent without pretending to be local-run.

## Authentication and credentials

The provider owns an LLMAuthSession: an async callable returning its current opaque authentication value, with awaited aclose. `LLMSessionOwner` calls that getter before authenticated operations and never parses or persists the returned token. Consumers share the owner across their runtime/credential binding. Concurrent initialization coalesces; attended mode does not secretly initiate unattended login. Absent optional browser authentication permits no-op providers to execute without creating a login browser. Scoped browser-free password-manager acquisition for the included API-key provider remains a follow-up.

Credential readers enforce declared role, selected index and username/password/TOTP kind. The saved item name is not necessarily the username. Helpers retrieve username on demand; readers do not cache or log credentials. The LLM reader is already scoped to its provider role. Helper subprocesses have closed stdin, bounded execution and argv commands without a shell. Username retrieval failures suppress raw identity-bearing diagnostics. Versioned worker IPC, live-operation revocation and full helper wiring remain follow-ups.

BrowserSession holds browser, context and current page. `BrowserOwner.prepare` invokes the UI hook once and yields that same session. Ordinary LLM integrations, unit/help/collection allocate no frontend. Explicit UI-provider integration and the next local/E2E consumer are specified in [prepared UI sessions](../prds/implementation_plan_i2c.md); they invoke the selected UI lifecycle and optional preparation, without making discovery request UI. Local frontend launch remains gated by RUN_UI. Each E2E attempt requires a fresh UI session, headless unless DEBUG or E2E-only E2E_DEBUG inspection is enabled. The future Run local consumer must open a headed startup browser when RUN_UI or DEBUG is enabled, with home/status, optional tracing, selected flow and optional UI last; RUN_UI alone enables the optional frontend. That future consumer must keep every local authentication browser headed, including later lazy login with the UI disabled. This is not current Run local runtime certification: the direct provider factory retains its existing local RUN_UI/inspection policy for automated login, and the future local executor owns the headed/inspection policy. Automated test-mode LLM login is headless without DEBUG inspection; attended authentication and the existing bounded visible human/hardware-security handoff remain exceptions. RUN_UI applies only to optional Run local frontend selection. E2E_DEBUG owner/selector compatibility does not implement an E2E executor; that future executor must share effective DEBUG across UI/LLM/backend retention before claiming complete E2E behavior. Operation consumers supply this policy to the managed browser factory. Inspection owners retain contexts, browsers and drivers until explicit cleanup; actual worker survival/inspection protocols remain open.

## Deadlines, failures and streams

The selected UI consumer uses [prepared sessions](../prds/implementation_plan_i2c1.md):
optional `prepare_session` advertises async `aprepare_session(*, session,
user_index, credentials) -> None`. Omission is anonymous readiness. Preparation
keeps the original browser/context and may select another live page in that
context. Optional `capture_evidence` and its trusted original-pixel safety policy
are defined in [UI evidence](../prds/implementation_plan_a4a.md). Core never
invokes an additional legacy authentication hook alongside preparation.

`OperationContext(correlation_id, deadline)` is immutable with an optional absolute monotonic deadline. Pass the same context through authentication, getter calls, operation work and stream iteration; retries never receive a fresh overall budget. Cleanup uses a separate bounded wait. Scheduled work at an expired deadline is conservatively possibly_executed; a fresh unscheduled coroutine is not_started.

OperationalError has a safe kind, execution disposition and optional retry delay. Kinds are authentication, authorization, unavailable, rate_limited, unsupported, invalid_request, deadline and unknown. Execution is not_started/possibly_executed/completed. Unknown failures are sanitized and possibly_executed. Retry authority belongs to the operation consumer. Shared completion consumers apply the bounded recovery policy below; mutation and workflow retries remain separate.

`ProviderAccessError.origin` defaults to `provider`. Generic result validation
sets `local_output_validation`, preserved through setup IPC, to distinguish
locally confirmed output rejection from upstream error-body codes and types.
Adapters must not populate this marker from upstream fields. The tool-conversation
owner can request a new proposal for confirmed invalid tool arguments within its
existing round limit; it never dispatches or replays the rejected batch. Local
validator unavailability, authentication and relevance rejection remain terminal.

Use `async with owner.stream(operation, context=budget) as stream` and iterate inside that lease. Do not treat a bare async iterator or garbage collection as ownership. Exhaustion or explicit awaited close releases iteration work; early break still requires context exit. Concurrent iteration is rejected. Cancellation-resistant factories, reads and close tasks retain their session dependencies until disposal is confirmed.


## Bounded completion recovery

The shared completion owner retries confirmed upstream HTTP 429 and 500..599 and
explicitly classified transient transport errors (`OperationalError.kind = network`), with
`LLM_MAX_ATTEMPTS` defaulting to five total attempts including the first (1..20;
one disables transport retries). Nominal delays are 200ms, 1s, 5s and 25s with bounded jitter
and a 30-second algorithm cap. A valid Retry-After minimum may be longer, but all
attempts and waits share the provider/enclosing deadline. Authentication, tools,
whole workflows and already delivered streams are never replayed. Provider and
SDK retry loops must be disabled on this shared path.

For a confirmed HTTP failure, retain the actual integer upstream status as
`error.http_status` on the raised safe provider error. Its ordinary `status` may
also describe provider error details; a status in an HTTP 200 body or stream does
not establish an HTTP failure. Preserve the confirmed status and any parsed
`error.retry_after` before reading or decoding a failure body. The shared
`api_agent.providers.inference_retry.retry_after` helper parses the header into
a bounded numeric hint; an unsupported large server minimum declines recovery.
Local validation, cancellation, local deadlines, TLS configuration failures,
pool saturation, unknown statusless failures and other HTTP statuses stay terminal
except for the separate output-limit admission correction below.
Providers classify typed connection/read/write-timeout/disconnect causes, not raw
messages or a generic `unavailable` kind. Network retries preserve possible prior
execution and unknown charges; an empty successful result is not a network error.
An underlying native socket timeout or typed TLS EOF does not turn a transport
failure into a local deadline or certificate/configuration failure.

Return available usage/billing on results and failures without inventing zero
for unknown charges. The shared consumer records one logical call with distinct
physical attempts, preserving partials and unknowns. Recovered attempts attract
yellow attention without changing application-defined attempts or Max passes.
See [recovery requirements](../prds/implementation_plan_f1b.md) for evidence,
accounting and acceptance boundaries.

## Output ceilings and admission correction

`LLM_MAX_OUTPUT_TOKENS` defaults to 200,000 per generation. Effective requests
use the smallest configured, caller-specific, advertised model and learned
output ceiling. Providers without output-limit support omit the implicit control.
For implicit ceilings, catalog listing is optional: a confirmed catalog-only 403
may leave model metadata unavailable while the declared provider capability
still supplies the configured ceiling. This does not bypass authentication,
inference authorization, explicit-budget checks or existing cache-hint rules.

An adapter may raise `api_agent.providers.output_limits.OutputLimitRejected`
only for a confirmed pre-inference HTTP 400/422 validation rejection stating a
smaller positive output maximum. Core does not parse arbitrary provider prose.
Shared ownership caches the maximum for the exact model within its selected
provider/account/endpoint/API owner, bounded to 256 entries and governed by
`LLM_OUTPUT_LIMIT_CACHE_TTL_SECONDS` (3,600 seconds; zero disables reuse).

One immediate corrected request may add one physical attempt independently of
`LLM_MAX_ATTEMPTS`, including when that setting is one. The original deadline,
cancellation, selected identity and other generation options remain authoritative.
Keep distinct attempt evidence and recovered attention; never replay tools or
an already generated/truncated response. Unknown charges remain unknown.
See [output-ceiling requirements](../prds/implementation_plan_f1d.md).

## Shutdown and failed loads

Closure rejects new work and waits for shared owned disposal. `CleanupPendingError` exposes retained owners and an awaited retry callable. Runtime pending API/UI/session owners retain their required clients; pending descendants retain tracing. Independent siblings may close. Operation cleanup waits for listener, then runtime, then prerequisite lease release. A pending lease blocks a new operation on that coordinator.

Failed factories may still own resources. `ProviderError.retained_owners` and cancellation ownership information preserve rollback handles; runtime adopts them. Isolated modules stay available until actual disposal succeeds. Caller cancellation must not abandon shared disposal. Failed or timed-out cleanup does not report success or remove retry handles.

## Review limits

Deterministic units exercise synthetic transports, browser/process doubles and local configuration; they make no real provider call. The prior checkpoint's 1,656-unit result describes the earlier foundation only. Current joined metrics and independent review evidence are recorded outside this public document.

Real adapters, selected-provider dependency manifests, installed-pack relocation, declaration authority, callback forwarding, worker IPC/debug survival and TUI/runner consumer wiring remain tasks. These contracts do not authorize live login, inference or E2E acceptance.
