# Included Studio host

Run `node apps/api-agent-ui/launch.mjs` with `UI_LISTENER_URL`,
`AGENT_DJANGO_URL` and an `API_AGENT_UI_LAUNCH_TOKEN` (20–128 URL-safe
characters). HTTPS additionally requires `API_AGENT_UI_TLS_CERT` and
`API_AGENT_UI_TLS_KEY`. The provider supplies these through the runtime process
owner. Node's standard library is the only host dependency.

Only explicitly listed public assets are served. `/config.json` exposes
`apiOrigin` and, when supplied by composition, an ephemeral `apiHeaders` session
capability. `API_AGENT_UI_ACCESS_TOKEN` accepts 32–128 URL-safe characters; this
is separate from readiness identity and is never saved in conversation storage.
Requests to the API go directly to the configured origin. The host performs no
inference and does not proxy API requests. SIGINT/SIGTERM stop the listener and
close its connections. Source and installed package startup do not depend on the
current working directory.

The browser consumes the shared [application chat HTTP contract](../../docs/architecture/application_http_v1.md),
including named SSE, heartbeat and cancellation. Composition must supply an
active backend; absent capabilities return HTTP 501. A retained test browser does
not itself extend the test provider's execution lifetime.
