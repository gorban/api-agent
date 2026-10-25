# Managed development tools and external sidecars

[Tool requirements](implementation_plan_o4.md) · [Ownership](implementation_plan_o4e.md) · [Infrastructure foundation](implementation_plan_o1.md) · [Production boundary](implementation_plan_h4.md)

These generic requirements cover a selected tool that uses an HTTP sidecar. They supplement the local service-output contract without making every tool network-based or every sidecar application-required.

## Endpoint and lifecycle authority

- [ ] Give a network tool a local address slot, bound by its owner to an ordinary typed setting. A configuration name need not end in `_ENDPOINT`; compatibility aliases must be explicit. The factory consumes the local slot, not a globally hardcoded API endpoint.
- [ ] Permit one stable blank declaration default in all environments. For development/test operations authorized to provision services, the selected instance may define blank as its managed-image policy. Acquire the declared pinned image and dependency closure, satisfy readiness/isolation, then supply its effective endpoint. No user endpoint value is needed for that branch.
- [ ] Include the tool and its prerequisites when structurally declared by the selected graph/test closure, even when a particular turn might not execute that branch. Unselected tools do not start containers or add settings. A nonempty explicit endpoint can select an externally managed service without starting a local duplicate; alternative owner policies require explicit declarations.
- [ ] Preserve configured value/source separately from the acquired host/container address. Never save a discovered address to dotenv or replace the user's blank intent with a transient value. Active ordinary endpoint fields remain editable; implementation selectors remain hidden by default.
- [ ] In the agent-only deployment profile, consume sidecars as external services. No endpoint value, including blank, authorizes local Docker/Compose, image preparation or ownership of deployment-managed containers. Deployment requirements may demand an explicitly supplied nonempty environment value. A declaration default, including a nonempty owner default, must not satisfy that explicit-source requirement.
- [ ] Validate missing, blank, whitespace-only or otherwise invalid required deployment addresses before readiness. Apply source-aware/nonempty validation in addition to ordinary field presence. Keep the declaration default blank; do not invent a production-specific localhost default. Validate the endpoint according to the admitted transport/owner policy, and report how to provide it without exposing credentials.
- [ ] Distinguish an optional instance from an optional dependency: a tool absent from the selected graph has no endpoint requirement, but a selected network tool cannot run without either its managed ready output or its valid externally supplied address. A local production sidecar still has an address explicitly provided by deployment configuration.

## Bounded isolated-container support

- [ ] Extend service declaration/admission only as needed for owner-required isolation. Support an explicitly selected and validated installed container runtime through a versioned bounded policy; retain image digest pinning, rootless/privilege boundaries, controlled mounts/ports and existing engine/lease/cleanup rules. Unknown settings or unavailable isolation reject before tool execution rather than falling back to a less isolated runtime.
- [ ] Keep runtime selection and verification with the trusted engine/platform owner. Do not treat an arbitrary service health string as proof of isolation, silently change host daemon defaults, or import executable runtime setup hooks from tool metadata. Where an owner requires gVisor, qualify the selected engine/platform against its [public Docker runtime contract](https://gvisor.dev/docs/user_guide/quick_start/docker/).
- [ ] Record any additional required read-only filesystem, privilege, capability, network or resource controls as individually typed/admitted features with rejection checks. Do not replace the Compose allowlist with unrestricted pass-through. Prebuilt image entrypoints remain supported without introducing arbitrary host commands; image build/release is a separate owner responsibility.

## Platform qualification and recovery

- [ ] Deliver [macOS/Linux engine qualification and minimal engine-aware container recovery](implementation_plan_o4f1.md). A selected named profile remains discoverable after a crash; short intended run lifetime does not waive cleanup recovery.

## Acceptance

- [ ] Selected development tool plus blank setting acquires its pinned image, waits for verified readiness/isolation, and receives the managed address; explicit external address starts no duplicate container.
- [ ] Same blank/default configuration in deployment rejects with an actionable required-environment diagnostic and zero orchestration effects. Explicit sidecar address connects without installing or launching container tooling; shutdown does not stop that external sidecar.
- [ ] Graph/test selection and endpoint edits rebuild the plan; unused tools require no endpoint or engine. Original blank provenance survives acquisition and compatible reuse; cleanup remains scoped and retryable.
- [ ] Required runtime missing, unsupported platform, forged isolation evidence, incompatible retained container and conflicting service identity reject without silent fallback. Complete applicable coverage, causal rejection tests, controlled lifecycle/installed proofs and independent/human gates before claiming container support.

This specifies required support, not delivery. The existing bounded Compose foundation does not yet admit explicit runtime selection; complete that extension before accepting a tool that requires it.
