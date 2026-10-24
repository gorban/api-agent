# Local startup, terminal output and shutdown follow-through

[Dashboard startup](implementation_plan_k2.md) · [Local operation](implementation_plan_h1a1.md) · [Diagnostic output](implementation_plan_b1c4a.md)

- [x] Default TRACE_VIEWER_URL to an empty string so Review configuration offers the ordinary default-acceptance action. Verify acknowledgement, dotenv persistence and restart without recurring review. Preserve managed Jaeger derivation and explicit custom viewer precedence.
- [x] Replace the earlier interactive dashboard credential display: the TUI must neither request nor render the LangFuse account/password. Under NO_STD_IN, display the configured dashboard account/password through terminal status on stderr only. Keep them out of structured stdout, shared events, home/status JSON, traces and test artifacts. Deliberately redirecting stderr also redirects that literal status; it is not a scrubbed event channel.
- [x] Terminate readiness/status text with a newline before structured diagnostic events arrive. Preserve valid JSON-line boundaries, ordinary selected-source logs and stdout/stderr ownership.
- [x] Show an early startup list and a visible shutdown list before waiting for owned work. Include agent, enabled UI/API and admitted infrastructure service/profile names, including pack contributions, without hardcoded database types or credentials. Distinguish selected components from resources actually acquired; a remote endpoint alone is not a managed service.
- [x] Diagnose the reported NO_STD_IN streaming-chat HTTP 502 through the real local composition and selected-provider lifetime. Add a causal regression for any proven defect and preserve correct interactive behavior. Synthetic successful requests do not close a provider-specific reported failure; record any unavailable evidence without claiming repair.
- [x] For interactive Stop/confirmed Quit and terminal Ctrl+C, settle existing cleanup and asynchronous-context exit tasks before the launcher discards its loop. Repeated cancellation must not race asynchronous-generator closure or abandon owned resources. Preserve actual cleanup failures, bounded timeout policy and actionable retry ownership; do not suppress CleanupPendingError to manufacture success.
- [x] Close independent source/causal review, applicable exact line/branch coverage, installed/package and public-boundary checks, then external review before the next human checkpoint.
- [ ] Human retest: accept the empty viewer default, verify interactive versus terminal credential presentation, startup/shutdown lists, terminal message streaming and one Ctrl+C cleanup.

## Explicit provider recovery

- [x] After a setup/authentication checkpoint fails, allow the next explicit Settings/Send action to retire that failed generation and create a fresh provider/session from the unchanged selected configuration. Preserve the provider's account-binding checks; introduce no account, funding or provider fallback. Generic shared-owner authentication failures remain sticky within their generation.
- [x] Finish bounded, acknowledged cleanup before admitting the fresh generation. Pending cleanup, cancellation or allocation failures retain usable ownership and block replacement. Concurrent actions share one recovery; active requests keep their admitted generation through completion and accounting. Preserve inspection, credential-redaction registration and accumulated usage.
- [x] Never replay an inference automatically or recover midway through the action that failed. Prove the failed action executes at most once, a later explicit action can recover, and completion/catalog transport failures do not silently trigger unrelated authentication replacement. The original live initiating exception remains unproven unless separately observed.

## Bounded cleanup attempts with retained execution

- [x] Bound each local cleanup attempt to 15 seconds. On expiry, report cleanup pending with retained owner/task and retry access; poll the same callback rather than starting another close concurrently. Distinguish a finite attempt from guaranteed finite process exit.
- [x] Retain the creating event loop while cleanup is pending. Interactive Stop/confirmed Quit and NO_STD_IN must show pending status and retry visibly until acknowledgement; do not discard an active asynchronous-context exit or silently force process exit. Repeated interruption and launcher handoff must preserve this ownership policy.
- [x] Verify stalled and cancellation-resistant callbacks, real operation context-manager exit, ordered later retry and normal shutdown. Preserve failure receipts and prevent cleanup success from being reported before all owned work acknowledges completion. Human acceptance remains separate.

## Verified checkpoint

Independent source and causal reviews, external review, package/installed checks
and public-boundary checks are complete. All 14,261 existing unit cases passed;
one appended deterministic cancellation regression also passed. The source-bound
full-plus-focused coverage union reaches exact 100% line and branch coverage;
this does not claim a single combined run after the new case was appended.
A genuine cached-provider Studio request returned the expected response with one
inference call, retained accounting and clean shutdown. The historical initiating
exception and original interruption cause remain unproven.

**Deferred human review**

Substantive implementation and independent/external review are complete. Explicit
human acceptance remains unchecked above; earlier partial acceptance is preserved.
