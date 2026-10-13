# Executable installed-provider conformance

[Consumer conformance](implementation_plan_m3c.md).

## Required behavior

- Verify a genuinely installed public wheel, not only archive extraction. Run an isolated consumer from an unrelated working directory with no source checkout on its import path. Assert framework imports and package metadata identify the installed distribution, and that original synthetic pack/provider source is unavailable before execution.
- Install or relocate an independently authored synthetic resource pack containing its manifest, configuration, selected factories, provider-local relative imports and dependency descriptors. Discover declarations without executing factories; then execute startup, one deterministic provider invocation and awaited shutdown through existing version-1 loading/lifecycle contracts.
- Preserve resolved pack/provider ownership, explicit setting precedence, capability validation, private module isolation and cleanup. An inactive synthetic provider must not import or run. No real authentication, browser, inference or network service is needed.
- Verify both successful disposal and a relevant failed-start/invocation cleanup path; reject a closed consumer and invalid factory/capability. Assertions must demonstrate actual invocation and module/resource retirement, not merely a zero process exit code.
- Confirm installed dependency descriptors describe only selected providers. This does not complete future operation-specific dependency preparation, native platform guarantees or UI/LangGraph consumers.

## Acceptance

- [x] Reproducible disposable installation proof and permanent deterministic controls for ownership, relative imports, invocation and disposal.
- [x] Independent adversarial review with isolated mutations detecting source-path fallback, omitted invocation and omitted disposal.
- [x] Applicable unit tests at exact 100% measured line/branch coverage, wheel and public-boundary/metadata checks.
- [ ] Human review of the executable evidence and remaining conformance limits.

## Verified scope

Five deterministic tests build a local wheel from declared development tools,
create clean standard-library virtual environments and install it using pip
with no index, dependencies or cache. Isolated consumers verify distribution
metadata and module origins, run from an unrelated directory and cannot use the
removed original synthetic pack. The relocated provider imports its local
helper without changing global import paths. Explicit CLI values retain priority
and installed selected manifests expose the expected dependency descriptors.

The existing `aload_provider` consumer starts, invokes and disposes one
synthetic API adapter. Controls cover invocation failure, invalid factories,
invalid capabilities, closed-consumer rejection, idempotent disposal and module
retirement. An inactive provider remains inert. This proof does not execute the
Django Runtime, an operation runner, UI or an external service; broader installed
consumers and operation-specific dependency preparation remain separate.

All subprocesses use the shared timeout budget and fixture-owned home/cache/temp
settings. Host Python/pip configuration cannot redirect installation or supply
source imports. A real hostile-environment test and isolated one-setting
mutations detect both target and config redirection. Independent review passes
five cases and rejects eight source/invocation/disposal/module/installer
mutations. The existing MIT-licensed build backend is also declared in the dev
extra so offline test builds need no host build-tool cache.

The full public gate passes 5,818 units plus 27 subtests, exact 100% lines/branches across 132 measured files (17,520 statements, 6,442 branches). The wheel, whitespace and 394-file indexed public-boundary audit pass. Three unrelated fork diagnostics remain; the preceding cache lifecycle repair leaves no SQLite ResourceWarnings. No production source repair was needed.
