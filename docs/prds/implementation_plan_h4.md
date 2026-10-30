# Agent-only production artifact

[Infrastructure boundaries](implementation_plan_h.md) · [Workflow contracts](implementation_plan_b1b1.md) · [Later build-time optimization](implementation_plan_h4a.md)

These requirements precede image acceptance. The production artifact serves the agent HTTP API and executes its selected workflow. Local development/test orchestration remains a separate capability.

## Runtime boundary

- [ ] Provide a documented noninteractive production entrypoint with explicit listen/configuration, startup/readiness and bounded SIGTERM draining. It must not enter the TUI, dependency bootstrap, test runner, browser inspection or local-service orchestration. Preserve cancellation, provider/session cleanup and configured telemetry flush.
- [ ] Install only production runtime and explicitly selected provider dependencies. Separate terminal/development extras and their import paths; prove startup without terminal UI, browser tooling, container CLI/socket, Node tooling, test/report generators or local prerequisite managers. A selected provider must support its declared noninteractive deployment credentials; reject unsupported interactive preparation without launching a browser or installing tools.
- [ ] Consume upstream tool/API, database and optional tracing destinations as externally provisioned endpoints. Do not start or stop their servers or sidecars. The agent's own HTTP API remains required. Optional trace export may run in-process; collectors/viewers do not. Keep credentials outside image layers and graph artifacts.
- [ ] Keep optional repository-owned tool/MCP services as separate images/targets with their own entrypoints, dependency closures, probes and shutdown. Shared source does not require a shared Dockerfile or build context. Record protocol/version compatibility and release inputs; the main agent never implicitly starts those services.

## Immutable graph identity

- [ ] Select one graph and resolved ordered replacements for each release. Capture its full declaration/prompt/schema/native-binding closure during the build, package only the admitted executable representation and required runtime data, and retain a secret-safe manifest of logical paths/content hashes, compiler versions, pinned runtime/provider/pack artifacts and non-secret definition-affecting configuration. Runtime endpoints, credentials and permitted request settings remain separately identified; they cannot silently change graph selection or bindings.
- [ ] Build only from clean committed definition inputs and pinned dependency/base artifacts. Keep the commit-SHA tag, enforce its non-reassignment release policy and record/deploy the image digest. In a multi-artifact composition, record every contributing revision/digest; one repository SHA alone does not identify all inputs. A graph change requires a new committed release identity.
- [ ] Compile the selected declarations during the build. At startup validate the resulting versioned immutable executable artifact and reuse one admitted plan per process, with fresh request/conversation state. Disable development watching and definition reload in this deployment profile; pin retries/resumptions to the admitted definition. Reject mismatched manifests, graph-changing runtime overrides and substituted source mounts. Use read-only definition paths, with explicitly separate writable state/cache paths. Preserve mutable development freshness in its own profile.

## Proof and delivery

- [ ] Test actual no-push image builds and relocated containers without development mounts, using controlled external endpoints. Prove API readiness, workflow selection/overrides, streaming/cancellation, provider failures, shutdown, independent sidecar reachability and absence of local service/browser/terminal startup. Reject dirty/missing/stale inputs and altered graph identity. Require exact applicable coverage, meaningful mutations, packaging/licenses, image/index/metadata review and human acceptance before deployment claims.

Multi-stage compilation and declaration/viewer exclusion belong to the correctness baseline. Choosing generated Python over a versioned compiled plan remains a later measured optimization decision.

## Deployment links and source exclusion

- [ ] Keep the frontend independently deployed. The agent home may expose a safe configured nonempty `UI_LISTENER_URL` as an external navigation link regardless of RUN_UI; it does not start, proxy, authenticate, health-certify or own that UI deployment. Configure a browser-reachable deployed URL rather than retaining a localhost development default. Preserve selected-provider browser-origin and authentication contracts separately from this link.
- [ ] Restrict matching UI startup to local orchestration with RUN_UI enabled (and separately declared test preparation). DEBUG alone opens local inspection but does not enable the UI. Production ignores development browser/startup behavior and never provisions the frontend or sidecars.
- [ ] Exclude every `.md.html` declaration, generated workflow SVG, viewer script/page and source-display asset from the final agent image and all its distributed layers, including bundled provider/pack archives. Use a separate build stage/source context; deleting files from a later layer is insufficient. Retain only required executable graph/prompt/schema data, native implementations and required licenses. No runtime declaration parsing or source mounts may restore excluded resources. Keep authorized source/history artifacts outside the deployed image.
- [ ] The production agent exposes no flow link, viewer listener, diagram/declaration download route or source/debug projection endpoint, even if RUN_UI or DEBUG is supplied. Do not reuse the local viewer host or status link set in production. Verify absent assets and routes in image layers and the live container; attempts to enable development viewing must fail before starting it. Preserve ordinary health/readiness and API operation.
- [ ] Prove build-time artifact parity against the source compiler for selected overrides, routing, prompts, retries/deadlines, streaming, cancellation and errors. Validate artifact/version/hash compatibility before readiness, without authentication or inference during compilation. Select the bounded executable artifact format before implementation; unsafe executable-object deserialization is not an acceptable shortcut.

## Tracing failure during deployment shutdown

- [ ] Qualify [runtime-image shutdown with failing trace delivery](implementation_plan_h4b.md); earlier export errors must not indefinitely block a pod stop after actual disposal is acknowledged.
