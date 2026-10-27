# Bedrock public workflow checkpoint

[Bedrock requirements](implementation_plan_f4.md) · [Workflow acceptance](implementation_plan_f4b.md)

The checkpoint is ordinary launch-owned inference. On 2026-10-25 the user reports
Ctrl+V works and the existing attended Bedrock integration test passes. Exact
configuration and token-usage receipts remain open. Authentication alone, catalog
discovery and standalone transport fixtures do not qualify inference.

## Requirements and remaining acceptance

- [x] Complete the [existing integration test](implementation_plan_f4e1.md) through
  attended selected-provider setup with bounded generation and nonempty completed
  inference, as user-reported. This exercises the provider contract directly;
  live Run local through the parsed graph remains separate. Never substitute
  another provider, model or credential source.
- [ ] Record a successful real public workflow inference under the separately
  approved spending scope. Record only selected factory, API mode, region, model,
  terminal outcome and normalized token usage; omit account identifiers, secrets,
  request bodies, session headers and raw authentication output. Unknown charges
  remain unavailable. A returned model identity is provider-reported evidence,
  not independent proof of routing.
- [ ] Keep shared runtime, Studio and lifecycle behavior provider-neutral. Use the
  existing attended password-manager interaction for recovery. Retain ownership
  through cancellation and cleanup; do not clear credential caches to force login.
- [ ] After the real workflow checkpoint, present the result and known limits for
  human review. Broader authentication, transport and production gates remain in
  their owning requirements; this checkpoint does not waive them.

## Ordinary Run local procedure

Prepare the normal source environment and frontend assets using the
[launcher instructions](../../README.md), then select the factory and supported
authentication/model settings in the [provider guide](../../src/api_agent/llm_providers/bedrock/README.md).
Run `./launch.py --no-std-in=False --start=False`, open **Review configuration**,
set `RUN_UI=True`, retain the
included Studio UI provider, and confirm an unambiguous supported
`LLM_DEFAULT_MODEL`. Review effective settings before choosing **Run local**.
Complete any existing attended recovery prompt, open the Studio browser
destination, select the intended catalog model and submit the approved prompt.
Use **Stop** or **Quit** to finish owned cleanup.

Studio's model picker and **Response metadata** expose catalog/requested and
reported model information. Provider selection is visible in configuration;
model names alone do not identify a provider. HTTP responses and bounded local
receipts retain validated usage; Studio metadata does not display token totals.
Use the [integration usage summary](implementation_plan_f4e2.md) for reviewable
token evidence. The next checkpoint uses the [attended integration procedure](implementation_plan_f4e1.md)
to complete a successful real request after session recovery.
See [acquisition](implementation_plan_f4e3.md), [clipboard](implementation_plan_f4e4.md), [CLI error](implementation_plan_f4e5.md) and [native input](implementation_plan_f4e6.md) guidance.
