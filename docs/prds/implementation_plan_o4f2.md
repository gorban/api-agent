# Selected sidecar preparation and engine binding proof

[Sidecar requirements](implementation_plan_o4f.md) · [Engine requirements](implementation_plan_o4f1.md)

## Bounded interfaces

- [x] `sidecar_policy` preserves configured address and source. Local blank selects managed ownership; explicit HTTP(S) addresses select external ownership. Deployment requires a nonempty environment address and never authorizes preparation or container ownership. Unselected tools require no address.
- [x] Infrastructure fragments admit only the additional typed `isolation` field, version 1: `runtime = "runsc"`, nonzero numeric `user`, finite `memory_mb`, `cpus` and `pids_limit`. The planner emits fixed read-only root, capability drop, no privilege escalation and bounded temporary storage controls. Arbitrary Compose fields remain rejected.
- [x] Managed acquisition checks installed runtime availability before start. Readiness and retained-object adoption inspect actual engine configuration against the admitted isolation controls. Service health claims cannot substitute for those checks.
- [x] Trusted owners can prepare captured, bounded source bytes through `BuildRequest`, `SecretInput` and `prepare_image`. Secrets use temporary owner-only files outside the build context and fixed BuildKit secret arguments. No arbitrary host hooks, build arguments or declaration-executed setup are admitted. Trusted build definitions must separately avoid exporting secrets into layers.
- [x] Local build receipts identify local image content; they do not claim registry manifest identity or bypass existing digest admission.
- [x] `EngineTarget` freezes the absolute executable, executable digest, controlled transport and corroborated daemon/socket identity. `PrerequisiteManager` and container actions accept the same target. Known targets are recorded before acquisition and restored without discovery or startup. Container management provides explicit target selection with a short target-key suffix and selected marker so repeated labels remain distinct; stop confirmation and log readers retain the selected engine. Named-tool recovery requires the captured inventory authority, not a profile-name prefix.

- [x] Explicit `http-status` readiness admits only a loopback endpoint target and expected status, with bounded response bytes and deadline. It supports services with textual health responses. It makes no JSON identity claim; authenticated protocol acceptance remains a separate owner check. Existing `http` identity readiness is unchanged.

## Integration boundary

These are explicit opt-in interfaces for trusted operation owners. They extend existing selected-tool closure, immutable infrastructure plan, lease, readiness and cleanup interfaces. They do not replace named-tool selection or change its configuration precedence.

Call policy selection before preparation, secret resolution or engine discovery. Use `prepare_selected_sidecar` so only the managed branch resolves source, secrets or engine factories. Factory and build failures expose neutral diagnostics. Capture a target once; plan using its absolute executable and controlled environment, then pass it to the manager with a private target registry. Bound managers and container actions automatically namespace lease journals by corroborated daemon/socket binding identity and canonical transport below the supplied private ownership root. Incidental execution settings such as PATH or executable location must not split active-lease exclusion for the same verified binding. Keep the full captured target key for selection and execution validation. Bind ready outputs through the existing tool output binding; retain original configured values separately. External and deployment branches own no container lifecycle.

The existing Compose manifest-digest boundary remains unchanged. A locally built content ID needs an owner-specific startup adapter or a separately reviewed public declaration extension. No caller may reinterpret a local content ID as a registry digest.

## Deterministic proof and acceptance

Run `python -m pytest tests/unit/test_tool_policy.py tests/unit/test_tool_preparation.py tests/unit/test_engine_targets.py tests/unit/test_infrastructure_lifecycle.py tests/unit/test_tool_infrastructure_bindings.py` with the checkout package on the Python import path. Tests exercise policy, capture budgets, secret cleanup, installed-runtime rejection, engine inspection, existing selected dependency closures and same-ID objects on two explicitly selected synthetic engines. Mutation checks must demonstrate causal rejection guards.

- [ ] Qualify a real installed runtime, image architecture and workload protocol on each supported platform. Synthetic evidence proves orchestration and rejection behavior only.
- [ ] Prove an actual managed process crash and recovery with a real engine, plus macOS dual-engine coexistence and isolation.
- [ ] Integrate trusted operation-owner calls with the final application composition and container-management screen; preserve explicit target selection and unavailable states.
- [ ] Independent review and human platform acceptance remain required before claiming platform support.
