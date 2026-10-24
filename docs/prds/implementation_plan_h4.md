# Agent-only production artifact

[Infrastructure boundaries](implementation_plan_h.md) · [Workflow contracts](implementation_plan_b1b1.md) · [Later build-time optimization](implementation_plan_h4a.md)

These requirements precede image acceptance. The production artifact serves the agent HTTP API and executes its selected workflow. Local development/test orchestration remains a separate capability.

## Runtime boundary

- [ ] Provide a documented noninteractive production entrypoint with explicit listen/configuration, startup/readiness and bounded SIGTERM draining. It must not enter the TUI, dependency bootstrap, test runner, browser inspection or local-service orchestration. Preserve cancellation, provider/session cleanup and configured telemetry flush.
- [ ] Install only production runtime and explicitly selected provider dependencies. Separate terminal/development extras and their import paths; prove startup without terminal UI, browser tooling, container CLI/socket, Node tooling, test/report generators or local prerequisite managers. A selected provider must support its declared noninteractive deployment credentials; reject unsupported interactive preparation without launching a browser or installing tools.
- [ ] Consume upstream tool/API, database and optional tracing destinations as externally provisioned endpoints. Do not start or stop their servers or sidecars. The agent's own HTTP API remains required. Optional trace export may run in-process; collectors/viewers do not. Keep credentials outside image layers and graph artifacts.
- [ ] Keep optional repository-owned tool/MCP services as separate images/targets with their own entrypoints, dependency closures, probes and shutdown. Shared source does not require a shared Dockerfile or build context. Record protocol/version compatibility and release inputs; the main agent never implicitly starts those services.

## Immutable graph identity

- [ ] Select one graph and resolved ordered replacements for each release. Package its full declaration/prompt/schema/native-binding closure and a secret-safe manifest of logical paths/content hashes, compiler versions, pinned runtime/provider/pack artifacts and non-secret definition-affecting configuration. Runtime endpoints, credentials and permitted request settings remain separately identified; they cannot silently change graph selection or bindings.
- [ ] Build only from clean committed definition inputs and pinned dependency/base artifacts. Keep the commit-SHA tag, enforce its non-reassignment release policy and record/deploy the image digest. In a multi-artifact composition, record every contributing revision/digest; one repository SHA alone does not identify all inputs. A graph change requires a new committed release identity.
- [ ] Validate the selected closure at startup and compile one immutable plan per process, with fresh request/conversation state. Disable development watching and definition reload in this deployment profile; pin retries/resumptions to the admitted definition. Reject mismatched manifests, graph-changing runtime overrides and substituted source mounts. Use read-only definition paths, with explicitly separate writable state/cache paths. Preserve mutable development freshness in its own profile.

## Proof and delivery

- [ ] Test actual no-push image builds and relocated containers without development mounts, using controlled external endpoints. Prove API readiness, workflow selection/overrides, streaming/cancellation, provider failures, shutdown, independent sidecar reachability and absence of local service/browser/terminal startup. Reject dirty/missing/stale inputs and altered graph identity. Require exact applicable coverage, meaningful mutations, packaging/licenses, image/index/metadata review and human acceptance before deployment claims.

Multi-stage dependency/tool separation can ship with the correctness baseline. Replacing declarative execution with generated code follows the separate measured optimization decision.

## Tracing failure during deployment shutdown

- [ ] Qualify [runtime-image shutdown with failing trace delivery](implementation_plan_h4b.md); earlier export errors must not indefinitely block a pod stop after actual disposal is acknowledged.
