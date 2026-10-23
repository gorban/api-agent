# Tracing backend lifecycle and verification

[Tracing overview](implementation_plan_k.md). Startup browser tabs, automatic enabled-dashboard login and persistent interactive credentials: [dashboard requirements](implementation_plan_k2.md). The bounded dashboard and local workflow join is implemented; its new acceptance checkpoint is tracked in that child.

Current routing and attribution: [endpoint-only tracing and shared collector requirements](implementation_plan_k1.md). No reporting installation-ID/registration step is required.

## Completed declaration slice

- [x] Add optional TRACE_MODE/TRACE_TARGET with generic optional-review metadata, conditional local endpoint defaults, dynamic review updates, and LOG_CONTENT=False. Live exporters, content gating and Compose lifecycle remain tasks below.

## Live backend proof slice

- [x] Export a scrubbed synthetic span through the actual OTEL profile and retrieve its trace ID through Jaeger v3. Confirm private synthetic prompt/auth values are absent and stop only the diagnostic scope's owned container. This is backend diagnostic evidence, not a TUI-started integration or graph trace.
- [x] Restore the upstream-selected Chainguard MinIO dependency pinned by digest for the complete LangFuse profile. Verify actual authenticated span export/retrieval, content scrubbing, retained traces and reuse of all six services through production owners. Use explicit Docker Hub `library` namespaces for Postgres/Redis. These synthetic backend diagnostics do not complete graph instrumentation or TUI integration/E2E acceptance.

## Checklist

- [x] Make exporter shutdown completion checking atomic with cleanup-worker acquisition. Direct concurrent close calls must flush/shut down the provider once; verify the former check/acquisition interleaving with gated scheduling, not only repeated sequential calls.

- [x] Add lazy, lifespan-owned manual OTLP exporters for OTEL and LangFuse, sharing the runtime privacy boundary before SDK queuing. Disabled tracing imports no SDK; fixed internal service metadata, bounded flush/shutdown, streamed/discarded backend bodies and fixed diagnostics prevent content/credential leakage. SDK allocation, sanitized payloads and shutdown failure are deterministically verified; live export and graph/provider instrumentation remain below.
- [x] Declare LangFuse public/secret keys, using conditional local development defaults and requiring explicit keys for remote targets. The same resolved pair supplies local headless provisioning, authenticated readiness and trace export. Add an optional tracing dependency group; development test dependencies are prepared automatically by the launcher.
- [x] Implement prerequisite leases and bounded readiness/reuse/rollback with exact image/configuration matching, cross-process ownership and restarted-owned versus reused-running tracking. Local release stops newly started/restarted services only when no active lease remains; test release retains services. Actual operation hooks and live profile verification remain open.

- [x] Add full LangFuse v3-compatible and current Jaeger v2 Compose profiles plus LocalStack storage profile, validating exact upstream storage/headless initialization variable names and controlled ownership. Planner selects a local tracer only for its conventional IPv4/localhost default endpoint, skips custom/remote targets and rejects an explicit blank target when tracing is enabled. Config-only checks are complete; exporters, started/reused lifetime and real retained traces remain below.

- [x] Declare shared optional `TRACE_MODE` without a default: omitted/empty disables tracing; exact `LangFuse` or `OTEL` selects the backend. Declare optional `TRACE_TARGET` without an unconditional default. Show both fields in configuration review even when not provided. A missing optional field has a blank effective value and `Not provided` source, without a Required marker.
- [x] Use conditional target defaults: LangFuse `http://localhost:3000`; OTEL `http://localhost:4318/v1/traces` (OTLP/HTTP). Changing mode recomputes target and Source while target is still defaulted. An explicitly saved/environment/CLI target stays explicit when mode changes; unset it to resume conditional defaults. Preserve config precedence and validate backend/endpoint combinations before startup.
- [ ] Before the first Ok integration/E2E run, wire LangGraph, node and provider tracing to the selected backend/target. Disabled mode creates no exporter or tracer containers. Correlate session/suite/test/attempt identifiers and token metrics; flush on completion, cancellation and shutdown. LangFuse may require public/secret keys; declare appropriate conditional authentication configuration when implementing, with headless local provisioning and no secrets in trace payloads.
- [x] Design local tracing setup from the public [Langfuse Docker Compose documentation](https://langfuse.com/self-hosting/deployment/docker-compose) and [headless initialization documentation](https://langfuse.com/self-hosting/administration/headless-initialization). Pin the selected upstream release and use its documented dependency stack and initialization settings to provision a local project and API keys. Keep credentials out of committed configuration and trace payloads.
- [x] Add separate Compose `langfuse` and `otel` profiles. When tracing is enabled and the effective target equals that mode's local default endpoint, start/reuse its local stack; remote/custom targets start no local tracer. Use Jaeger all-in-one for the local OTEL route, exposing OTLP HTTP port 4318 and UI port 16686. Verify the selected supported image and pin it when implementing.
- [x] Follow [shared session lifecycle](implementation_plan_h2a2.md): retain local stacks after test completion for inspection while a session/operation needs them; stop managed tracer containers after the last session and active operation release. Keep volumes and traces on normal shutdown. Custom/production collector targets remain outside local management; preserve verified configuration and ownership during reuse.
- [x] Implement the [independent trace-content policy](implementation_plan_k1b.md) before the next application tracing join: TRACELOOP_TRACE_CONTENT controls both OTEL and LangFuse payloads, while LOG_CONTENT controls logs/local evidence. Both default False; credentials remain excluded. Verify all flag combinations before export.
- [ ] Add deterministic config/lifecycle/export tests and targeted human-reviewed integration checks for both backends before the Ok milestone. Confirm retained test traces remain viewable, subsequent tests reuse the stack, remote tracing starts no container, and local-run shutdown respects ownership.

Jaeger endpoint evidence: [official deployment ports and protocols](https://www.jaegertracing.io/docs/1.76/deployment/) and [current OTLP APIs](https://www.jaegertracing.io/docs/2.21/architecture/apis/). OTLP HTTP traces use `/v1/traces`; the browser UI is a separate endpoint.


## Independent content controls

- [ ] Complete the bounded [separate log/evidence and shared trace content controls](implementation_plan_k1b.md) verification and acceptance checkpoint. Broader automatic instrumentation and deployed-backend acceptance remain open.

## Bounded manual join checkpoint

The bounded manual workflow/client, dashboard and diagnostic join has completed
technical verification and external review. [Independent controls](implementation_plan_k1b.md),
[startup dashboards](implementation_plan_k2.md) and [local output](implementation_plan_b1c4a.md)
retain human acceptance and broader requirements. Automatic instrumentation,
metrics, propagation and deployed parity remain open.
