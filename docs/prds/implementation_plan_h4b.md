# Runtime image tracing shutdown qualification

[Agent-only runtime image](implementation_plan_h4.md) · [Shared disposal behavior](implementation_plan_k2b5.md)

- [ ] During the actual runtime-image qualification, send the container/pod its normal stop signal with a collector unavailable, refusing connections or rejecting exports. Confirm requests drain, ASGI shutdown completes within the admitted grace period after actual resource disposal, and the process exits without waiting forever for successful trace delivery.
- [ ] Preserve a fixed credential-free error log and failed-delivery outcome; do not report successful export. Verify both OTEL and LangFuse exporter routes under their own configuration. No local collector, viewer or development service may be started or stopped by the deployed agent.
- [ ] Keep actual live or failed disposal distinct from historical delivery failure. Exercise cancellation and shutdown timeout behavior against the chosen server/process supervisor, and document the container platform's final grace-deadline handling. Do not infer pod behavior solely from a Python unit or local ASGI check.
- [ ] Complete independent image review and human deployment acceptance.

The shared runtime fix and native ASGI signal proof precede this qualification; they do not complete the image/container acceptance criteria above.
