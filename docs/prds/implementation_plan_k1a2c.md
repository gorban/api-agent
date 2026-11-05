# Provider-owned tracing configuration

[Tracing providers](implementation_plan_k1a2.md).

## Selection and discovery

- [x] Supply a bundled default `TRACE_PROVIDER` through ordinary provider discovery. Its contributed `TRACE_MODE` selects disabled, LangFuse or OTEL behavior. An empty mode allocates no SDK, factory, network, browser or collector resources.
- [x] Keep static version-1 custom providers valid. Define bounded declarative profiles for providers whose signals, dashboard and infrastructure vary by a declared selector. Select identically from live and captured declarations; preserve descriptor source identity and bind profile and effective destinations into protected receipts. Keep backend selection out of core/TUI display logic. Dashboard declarations may name bounded viewer dependencies and separate managed-local provisioning/readiness dependencies. Viewer-only resolution validates the active dependencies without requiring unrelated exporter settings. Optional endpoint classification must not authorize infrastructure until required managed dependencies are resolved and the selected profile and destinations are revalidated; missing or invalid export-only controllers must not block an explicit remote dashboard.
- [x] Contribute backend settings through typed provider configuration. Review exposes only applicable fields. Keep saved inactive settings from the selected declaration removable through Clear `.env`, without executing factories. Preserve shared content and credential policy.

## Destinations and signals

- [x] Replace `TRACE_TARGET` with `TRACE_EXPORT_ENDPOINT`, always a complete ingestion URL. No compatibility alias is required. Defaults are `http://localhost:3000/api/public/otel/v1/traces` for LangFuse and `http://localhost:4318/v1/traces` for OTEL. Adapters must not append another path.
- [x] Keep dashboard URLs distinct. Derive managed-local defaults only for the exact admitted local ingestion/dashboard combination; custom remote ingestion must not acquire a local stack or guess a remote dashboard URL.
- [x] Resolve explicit configuration and permitted standard OTEL environment settings once, preserving source precedence and explicit empty values. Actual exporters, infrastructure plans, viewer configuration, test preparation and protected snapshot restoration must use the same effective destinations. Reject captured profile or destination drift before allocation.
- [x] Hide exporter, resource and trace-content settings while tracing is disabled. Reveal LangFuse authentication and local viewer settings only for their applicable route. Offer only supported HTTP/protobuf export; reject an explicit unsupported protocol actionably.
- [x] Keep LangFuse trace-only. OTEL may offer metrics opt-in to a separate explicitly configured metrics destination. Reveal endpoint, temporality and profile only when metrics are enabled; do not infer metric ingestion from Jaeger or a general OTLP base. Preserve custom provider capabilities and validation.
- [x] Preserve optional trace-failure isolation, shared observation capture, privacy, bounded flush/disposal before collector release, leases, retained volumes and dashboard-only/export-only configurations. No new metric backend, periodic delivery or retention reset is included.

- [x] Explain unavailable managed dashboards using the admitted provider selector, selected profile, actual safe ingestion endpoint and descriptor-owned managed defaults. Distinguish a stale saved endpoint after mode changes from an explicitly empty viewer URL; preserve explicit remote ingestion and suggest reviewing saved overrides or setting an explicit viewer URL. Never disclose credential-bearing URL components.

## Verification and acceptance

- [x] Verify disabled/LangFuse/OTEL/custom truth tables, Review/Help/Clear laziness, endpoint precedence and explicit empties, local/remote selection, capability rejection, synthetic third-party profiles, captured restore and tampered identity rejection. Include causal checks for independently enforced guards.
- [x] Complete paired independent reviews, applicable exact unit line/branch coverage, workflow/package/relocated checks, intended-source boundary/metadata/license review and final external review. Retain separate release gates when not exercised.
- [x] Human acceptance of trace configuration recorded November 3, 2026. The user approved the configuration checkpoint after interactive testing; automated custom-provider and content-policy qualification remains separately evidenced above.

## Dashboard diagnostic follow-up

Focused selection/viewer tests cover both cross-profile endpoint mismatches, explicit-empty viewer disabling and URL userinfo/query/fragment redaction. The effective selection forwards a generic capability-loss reason; presentation reads provider declarations without provider-name branches.

- [x] Complete the renewed full coverage and independent review gates for the diagnostic follow-up.
- [x] Human acceptance of the actionable unavailable-dashboard diagnostic, including the required-field ordering repair.

[Required-field diagnostic ordering repair](implementation_plan_k1a2c1.md) tracks a bundled profile mismatch that still produced the generic missing-configuration message.
