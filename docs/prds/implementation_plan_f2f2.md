# GPT-5.6 Luna reasoning capability

[Reasoning contract](implementation_plan_f2f.md)

## Requirements and bounded delivery

- [x] Recognize only the documented `gpt-5.6-luna` alias on the official OpenAI API route. The [model contract](https://developers.openai.com/api/docs/models/gpt-5.6-luna), verified October 18, 2026, lists native none, low, medium (default), high, xhigh and max; the shared enum exposes off, low, medium and high. Map off to native none in Responses and Chat Completions; omission preserves the provider default. Additional native levels remain outside the shared enum.
- [x] Advertise those common choices through the existing catalog without authentication or inference. Preserve rejection of explicit unsupported efforts, unknown aliases, unlisted dated versions and compatible endpoints. Official verification alone must not advertise OpenRouter support.
- [x] Preserve requested model and effort through worker transport and accounting. No model substitution, endpoint fallback or changes to in-flight defaults.
- [x] Complete internal independent review and joined validation of controlled catalog, native request and worker controls. Frontend interaction acceptance remains separate.

## Qualification boundary

The capability module participates in workflow sampling fingerprints. Changing it invalidates prior qualification; retain prior measurements and require separately authorized fresh qualification rather than replacing source hashes. This capability change does not authorize inference or paid sampling.

## Studio settings integration

- [x] A separately selectable integration starts the normal selected Studio session with a test-local GPT-6-Sol/medium backend default and the unchanged configured allowed-model policy. Before provider preparation, require the included GPT provider, supported official route, and admission of both exact required model IDs.
- [x] Before opening the UI, a named catalog prerequisite prepares the actual selected provider, requires unique canonical Sol6/Luna5.6 rows with exact family/version metadata and their required medium/off efforts. This mirrors normal pre-UI selection preparation without depending on the configured global default, substituting a catalog, changing the production fetch deadline or inferring before readiness. Runtime API requests still retrieve the real catalog.
- [x] Use the original page's Settings controls to choose the advertised GPT-Luna family, version 5.6 exact model ID, and off. Send “Respond with exactly: Maybe” once; require exactly `Maybe` after trimming surrounding whitespace only (case and punctuation remain significant), and exact outgoing model/effort differing from backend defaults. Runtime catalog or inference failures fail rather than skip or substitute a model.
- [x] Preserve normal usage/Allure capture, selected browser ownership, and DEBUG retention. Offline controls precede any separately authorized live smoke.
- [x] Complete internal independent review, renewed joined gates and the separately authorized live settings smoke. The real Settings flow selected version 5.6/off and returned exactly `Maybe`; one requested/actual model call reported zero reasoning tokens.
- [ ] Complete external PR review and frontend human interaction acceptance. The live automated settings smoke does not replace that acceptance.
