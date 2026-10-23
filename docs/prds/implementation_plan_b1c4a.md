# Local workflow diagnostic content and durable output

[Live diagnostics](implementation_plan_b1c4.md) · [Local operation](implementation_plan_h1a1.md)

- [ ] Extend Run local diagnostics with graph/component/node identity, activity and outcomes, including tool calls. Reuse admitted workflow observations and identities rather than independently inferring execution from UI-process output. Keep bounded status available without content capture.
- [ ] With LOG_CONTENT=False, show that input/output content is withheld. With True, permit credential-scrubbed user-visible node and tool inputs/outputs. Preserve the exclusion of authentication state, hidden reasoning and registered credentials. Apply privacy checks to each complete admitted payload before truncation, serialization or emission; arbitrary child stdout remains behind its separate streaming-privacy gate.
- [ ] Default to explicitly truncated input/output previews. Add a visible, default-false configuration control for full permitted diagnostic payloads, independently of LOG_CONTENT. Full output must not override content/privacy policy or expand model context by default. Document limits and omission markers, including payloads outside supported schemas.
- [ ] Keep terminal streaming and the TUI's bounded in-memory display separate. Full permitted output can be retained through a documented durable-output workflow while the console discards older display rows. Define output destination, backpressure, interruption and failure behavior before implementation; do not claim a nonexistent launcher option.
- [ ] Use the existing `--no-std-in` terminal mode with stdout piped through `tee` for durable output; do not introduce `--no-std-out`. Share admitted workflow identities and credential/content policy across console projections, the journal and tracing exporters. Reuse the existing OTLP transport for OTEL and LangFuse rather than reading events back from either dashboard. Dashboard startup/login and per-node export remain explicit integration work; a configured exporter alone does not establish that join. Channel-specific preview limits may differ, but must be disclosed and must not change execution identity or accounting.
- [ ] Verify content-off, preview and full-output states with large synthetic nested graphs and tool calls, split/encoded credentials and control characters. Prove displayed truncation cannot expose secret prefixes, slow consumers cannot silently defeat cancellation/cleanup, and event identity matches the shared live/recorded evidence contract. Renew applicable coverage, causal mutations, installed and boundary checks, independent review and human acceptance.

This is a follow-through requirement after the initial core Run local checkpoint,
not functionality delivered by its activity-only UI-process reader. Its durable
command is `--no-std-in` with `tee`; settle content and backpressure details before
dependent implementation.

## Non-content readiness and request events

- [ ] Include selected-provider setup/authentication needed, started, ready or failed; catalog acquisition/cache reuse/expiry/failure; and request accepted, executing, completed, cancelled or failed in the local agent log projection. These bounded lifecycle messages remain available with LOG_CONTENT=False, correlate with shared request/component identity where present, and never contain credentials, session state, account descriptors or raw provider errors. Use fixed safe categories and outcomes rather than arbitrary process text.
- [ ] Keep the projection tied to the shared runtime/tracing event source. Verify useful progress during authentication/catalog waits and failures as well as successful requests; prevent duplicate console rows from nested exporters and preserve bounded elapsed/error reporting. No rich lifecycle logging is claimed by the current readiness-only agent source.
