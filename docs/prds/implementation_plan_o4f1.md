# Platform engines, runtime qualification and cleanup recovery

[Sidecar policy](implementation_plan_o4f.md) · [Container management](implementation_plan_l.md) · [Lifecycle ownership](implementation_plan_o1.md)

## Platform qualification

- [ ] Qualify macOS development with a dedicated named Colima profile whose Linux guest supports the required isolation runtime. Resolve its actual Docker-compatible Unix transport and engine identity explicitly; do not retarget the user's default context/profile. Capture profile, guest architecture, runtime/platform and image compatibility. An already installed profile still needs current verification before adoption.
- [ ] Qualify Linux with the user's existing rootless Podman as the preferred candidate for owner-required gVisor isolation. Verify the actual CLI/client-server boundary and supported OCI-runtime selection path, installed server-side runtime, rootless/user namespaces, SELinux, cgroups, mounts/networking and resource controls against the intended image. A working default OCI runtime or Docker-compatible socket alone does not prove the required isolation.
- [ ] Prove a disposable controlled image can start with the required runtime, deliver the sidecar protocol, enforce required limits, and cancel/clean up through the same engine/Compose frontend used by the application. Distinguish an unsupported client flag from an unavailable server capability. No global runtime/daemon default may silently change.
- [ ] If the actual Podman path cannot satisfy the contract, record the concrete blocker and qualify one explicitly selected local Linux engine/profile that can. Keep the normal engine for other stacks. Never silently weaken isolation or launch against a different engine. Support and acceptance are measured per platform/architecture/transport/runtime combination.

Public contracts: [Podman OCI runtime selection](https://docs.podman.io/en/latest/markdown/podman.1.html), [gVisor rootless integration](https://gvisor.dev/docs/user_guide/rootless/), [Colima profiles](https://colima.run/docs/profiles/). These establish candidate mechanisms, not certification of any particular workstation.

## Minimal engine-aware management

- [ ] Keep one explicit engine target per operation as the default. Reuse existing verified CLI/socket binding and namespaced leases. Freeze its executable/controlled transport and corroborated identity through planning, acquisition, logs, status and cleanup; later ambient context/PATH changes cannot retarget those operations.
- [ ] Retain a small recoverable record of engine targets actually used for the selected checkout/pack's managed services, including required named profiles. Stable engine identity, privilege/storage namespace and container identity must distinguish objects; friendly profile names or container IDs alone are insufficient. Keep transport details outside public artifacts and diagnostics.
- [ ] Extend Container management with a simple selection among those known targets if more than one is used. Show one engine's scoped inventory/count/logs at a time; the selected engine must be clear. Aggregated multi-engine views, automatic scanning of all host contexts and cross-engine scheduling are not required.
- [ ] Bind inventory, logs, confirmation and stop-time revalidation to the selected recorded engine and existing ownership/lease checks. Namespace selections by engine plus container ID; an identical ID in another engine must not receive the action. Preserve hosting-container exclusion and refusal to stop another invocation's active lease.
- [ ] Treat stopped profiles/unavailable targets as explicit unavailable states, not empty successful inventory. Never substitute the current default engine, auto-start a VM or reset an engine just to populate the screen. Revalidate engine generation/identity after reconnect before acting on retained IDs.
- [ ] Normal operation cleanup remains primary, but crashes, SIGKILL, partial-start failures and deliberately retained debug/reused services remain discoverable after TUI restart. Run-scoped lifetime does not waive recovery. Container management stops eligible containers with existing confirmation; it does not delete volumes, stop a shared engine/profile, or manage deployment-owned sidecars.

## Acceptance

- [ ] Prove the macOS named-profile path and Linux qualified path separately, including unavailable runtime/profile, external endpoint bypass and no isolation fallback. Keep installation/configuration outside inert discovery; document any setup prerequisites explicitly.
- [ ] Test two known engines with scoped containers, identical container IDs, changed defaults, unavailable targets, stale daemon identity, active leases and hosting-container exclusion. Stop/log operations must affect only the explicitly selected engine and freshly verified objects.
- [ ] Kill a controlled operation without normal teardown, restart TUI and recover its owned leftovers on the recorded engine; retain ordinary cleanup/reuse/volume gates. Complete existing coverage, mutation, independent review and human checks before claiming platform or recovery support.
