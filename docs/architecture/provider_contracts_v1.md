# Provider contracts, version 1

The executable declarations are in [contracts.py](../../src/api_agent/providers/contracts.py), [LLM data](../../src/api_agent/providers/llm.py) and [operation control](../../src/api_agent/providers/control.py). This document records the implemented foundation. Distribution, declaration authority, worker inspection and credential IPC remain open in [consumer hardening](../prds/implementation_plan_m3.md); human acceptance of the concrete consumer contract is pending. API payloads remain adapter-defined. Core does not parse external authentication tokens.

## Factory and capabilities

Factories receive resolved configuration and runtime services and synchronously return an object with integer `version = 1`. Services carry verified TLS, emission boundaries and the declared path owner. Runtime startup and disposal own awaited lifecycle hooks.

`ProviderCapabilities` declares kind, supported operations, credential roles, an optional LLM role and optional LLM features. Kinds/methods are: API invoke/ainvoke; LLM describe/adescribe, complete/acomplete, stream/astream, usage/aread_usage; access describe/adescribe, usage/aread_usage; UI launch/astart; upstream validate/avalidate. Access adapters cannot advertise completion or streaming. Version-only foundation factories may load without claiming an operation. Only declared capabilities may be executed.

Use `aload_provider` inside an event loop. Synchronous loading rejects active-loop use before importing a factory. Optional lifecycle/authentication hooks must have their declared async shape and signature; route setup permits synchronous or asynchronous hooks. Streaming permits an async generator or a regular factory returning an async iterator. `require("llm", "stream")` checks that return value at invocation; discovery never calls the stream. Other selections return the adapter itself. The managed session stream also accepts an awaited factory; its iterator must provide awaited close.

## Common inference data

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

`StreamEvent` is text, tool_call or completed. The terminal completed event carries the result and terminal usage. Clean exhaustion requires exactly one terminal event; duplicate terminal events or output after it are invalid. Early explicit closure may have no terminal result and must not invent one. Text, tool arguments, schemas, result payloads and browser objects are excluded from carrier repr.

## Selection and activation

LLM_PROVIDER, API_PROVIDER, UI_PROVIDER and ACCESS_PROVIDER select file factories. There are no default external factories in public core. An API component may contribute UPSTREAM_API_PROVIDER. Selected nested declarations are discovered without executable imports and use effective-pack defaults plus explicit dotenv/environment/CLI overrides. Selection changes rebuild schema contributions and both configuration inventories. Component overrides marked override_if_present=true require override=true and apply only if the target field exists; this permits optional callback overrides without introducing fields owned by an inactive provider. Present targets still require compatible types.

Configuration discovery and executable activation are separate. `activation_kinds(values, required_kinds=...)` selects the operation's capabilities. API activation includes its upstream dependency. Explicit access-only or LLM-only consumers avoid unrelated factories and API identity loading. Default run-local/integration/E2E startup activates configured API/upstream/LLM/access owners; UI activates only for E2E or local-run with RUN_UI. No mode, help, unit, collection and configuration modes activate no executable providers. Callback-only consumers can request access routes without starting a frontend. This foundation does not implement callback forwarding or the next provider-session operation consumer; explicit UI integration activation must use a declared capability intent without pretending to be local-run.

## Authentication and credentials

The provider owns an LLMAuthSession: an async callable returning its current opaque authentication value, with awaited aclose. `LLMSessionOwner` calls that getter before authenticated operations and never parses or persists the returned token. Consumers share the owner across their runtime/credential binding. Concurrent initialization coalesces; attended mode does not secretly initiate unattended login. Absent optional browser authentication permits no-op providers to execute without creating a login browser. Scoped browser-free password-manager acquisition for the included API-key provider remains a follow-up.

Credential readers enforce declared role, selected index and username/password/TOTP kind. The saved item name is not necessarily the username. Helpers retrieve username on demand; readers do not cache or log credentials. The LLM reader is already scoped to its provider role. Helper subprocesses have closed stdin, bounded execution and argv commands without a shell. Username retrieval failures suppress raw identity-bearing diagnostics. Versioned worker IPC, live-operation revocation and full helper wiring remain follow-ups.

BrowserSession holds browser, context and current page. `BrowserOwner.prepare` invokes the UI hook once and yields that same session. Ordinary LLM integrations, unit/help/collection allocate no frontend. Explicit UI-provider integration and the next local/E2E consumer are specified in [prepared UI sessions](../prds/implementation_plan_i2c.md); they invoke the selected UI lifecycle and optional preparation, without making discovery request UI. Local frontend launch remains gated by RUN_UI. Each E2E attempt requires a fresh UI session, headless unless RUN_UI or debug is enabled. Unattended LLM login is headless unless RUN_UI or DEBUG makes it visible; attended login is always headed. The managed browser factory owns this policy. Inspection owners retain contexts, browsers and drivers until explicit cleanup; actual worker survival/inspection protocols remain open.

## Deadlines, failures and streams

`OperationContext(correlation_id, deadline)` is immutable with an optional absolute monotonic deadline. Pass the same context through authentication, getter calls, operation work and stream iteration; retries never receive a fresh overall budget. Cleanup uses a separate bounded wait. Scheduled work at an expired deadline is conservatively possibly_executed; a fresh unscheduled coroutine is not_started.

OperationalError has a safe kind, execution disposition and optional retry delay. Kinds are authentication, authorization, unavailable, rate_limited, unsupported, invalid_request, deadline and unknown. Execution is not_started/possibly_executed/completed. Unknown failures are sanitized and possibly_executed. Retry authority belongs to the operation consumer; these owners perform no automatic inference or mutation retries.

Use `async with owner.stream(operation, context=budget) as stream` and iterate inside that lease. Do not treat a bare async iterator or garbage collection as ownership. Exhaustion or explicit awaited close releases iteration work; early break still requires context exit. Concurrent iteration is rejected. Cancellation-resistant factories, reads and close tasks retain their session dependencies until disposal is confirmed.

## Shutdown and failed loads

Closure rejects new work and waits for shared owned disposal. `CleanupPendingError` exposes retained owners and an awaited retry callable. Runtime pending API/UI/session owners retain their required clients; pending descendants retain tracing. Independent siblings may close. Operation cleanup waits for listener, then runtime, then prerequisite lease release. A pending lease blocks a new operation on that coordinator.

Failed factories may still own resources. `ProviderError.retained_owners` and cancellation ownership information preserve rollback handles; runtime adopts them. Isolated modules stay available until actual disposal succeeds. Caller cancellation must not abandon shared disposal. Failed or timed-out cleanup does not report success or remove retry handles.

## Review limits

Deterministic units exercise synthetic transports, browser/process doubles and local configuration; they make no real provider call. The prior checkpoint's 1,656-unit result describes the earlier foundation only. Current joined metrics and independent review evidence are recorded outside this public document.

Real adapters, selected-provider dependency manifests, installed-pack relocation, declaration authority, callback forwarding, worker IPC/debug survival and TUI/runner consumer wiring remain tasks. These contracts do not authorize live login, inference or E2E acceptance.
