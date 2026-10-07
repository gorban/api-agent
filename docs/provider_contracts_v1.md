# Provider contracts, version 1

The executable declarations are in [contracts.py](../src/api_agent/providers/contracts.py). This document records the foundation currently implemented, not a frozen consumer-ready contract. Complete [consumer hardening](implementation_plan_m3.md) before concrete adapter/operation lanes depend on it. API payloads remain adapter-defined; shared LLM data/feature shapes still need specification. Core does not parse external authentication tokens.

## Factory and capabilities

Factories receive resolved configuration and runtime services and synchronously return an object with integer `version = 1`. Services carry verified TLS, emission boundaries and the declared path owner. Creating a factory does not authenticate or launch processes. Runtime startup and disposal own those actions through awaited lifecycle hooks.

`ProviderCapabilities` declares kind, supported operations, credential roles and an optional LLM role. Kinds/methods are: API invoke/ainvoke; LLM describe/adescribe, complete/acomplete, stream/astream, usage/aread_usage; UI launch/astart; upstream validate/avalidate. Only declared capabilities may be executed. Version-only foundation factories can still load without claiming any operation. The generic API carrier permits an adapter-defined payload; selected LLM identity is carried explicitly in `LLMRequest.selected_llm`. Result/event payloads and browser objects are excluded from repr.

Use `aload_provider` inside an event loop. Synchronous loading rejects active-loop use before importing a factory. Optional lifecycle/authentication hooks must have their declared async shape and signature; route setup permits synchronous or asynchronous hooks. Streaming permits an async generator or a regular factory returning an async iterator. `require("llm", "stream")` returns a forwarding view which checks that return value at invocation; discovery never calls the stream. Other operation selections return the adapter itself.

## Selection and configuration

LLM_PROVIDER, API_PROVIDER and UI_PROVIDER select file factories. There are no default external factories in public core. An API component may contribute UPSTREAM_API_PROVIDER; core does not impose that field globally. Selected nested declarations are discovered without executable imports. Contributions participate in the existing effective-pack defaults and explicit dotenv/environment/CLI precedence; changing selection must not reuse an old executable binding. Factory paths and nested selection roots are independent of CWD.

## Authentication and browsers

The provider owns an LLMAuthSession: an async callable returning its current opaque authentication value, with awaited aclose. `LLMSessionOwner` calls that getter before authenticated operations and never parses or persists the returned token. The operation consumer must share its owner across the selected runtime/credential binding. Concurrent initialization coalesces; callers' cancellation must not strand owners. Attended mode does not secretly initiate unattended login.

Credential readers enforce declared role, selected index and password/TOTP kind. Passwords are fetched only when needed and TOTP is fetched at the challenge; neither reader caches or logs credentials. The LLM reader is already scoped to its provider's role.

BrowserSession holds browser, context and the current page. `BrowserOwner.prepare` invokes the UI authenticate hook once and returns that same session. The reusable UI policy allocates no frontend for unit/integration/help/collection; local UI is headed and exists only with RUN_UI. The E2E consumer must allocate a fresh browser per attempt, headless unless RUN_UI or debug is enabled. LLM authentication owners reject headed browsers in all cases. Debug inspection ownership retains the browser driver as well as contexts and must be explicitly disposed.

## Shutdown and failed loads

Closure rejects new work and waits for shared owned disposal. Failed or timed-out cleanup retains the resource and its dependencies for retry; it does not report successful release. Caller cancellation does not abandon shared disposal, and retry does not repeatedly cancel a task already cleaning up. Owners enforce their creating process/event loop before invoking hooks.

A malformed returned adapter may still need disposal. `ProviderError.retained_owners` identifies those owners when rollback remains pending; cancellation carries the same ownership information. The runtime adopts them and retries cleanup. Direct loader callers must also await their disposal. Isolated provider modules remain available until actual disposal succeeds.

## Review limits

This batch verified foundation exports and owners with synthetic providers, injected browser/process doubles and local configuration. Scoped implementation review passed after lifecycle/precedence repairs and focused mutations. The checkpoint core suite passed 1,656 units with exact 100% measured line/branch coverage, and its wheel built with the exports. Subsequent consumer review identified additional contract decisions in the hardening task; stable-contract human acceptance remains pending.

This does not authorize live login, API traffic or E2E acceptance. Dependency installation, real adapter protocols, UI callback forwarding, live frontend authentication and TUI/runner consumption have follow-up tasks. Unit results do not complete those operations.
