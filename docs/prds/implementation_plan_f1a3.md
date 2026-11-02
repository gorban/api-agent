# OpenRouter integration defaults and routing

- [x] Supply provider-owned test defaults that match the OpenRouter model catalog when its API-key endpoint is selected; preserve explicit per-test choices, other endpoints and ordinary application settings.
- [x] Keep tool calls individually validated and executed in order when routing through OpenRouter, including responses containing multiple calls; preserve partial execution evidence on later failure.
- [x] Qualify supported structured-output routes and reasoning test budgets using real provider results while honoring the selected API mode and explicit output policy. Unsupported combinations must fail clearly rather than silently changing mode or weakening assertions.
- [x] Verify the normal selected integration suite, independent and external review, applicable coverage and public-boundary checks.
- [x] Repair the reported provider-owned cache-test credential admission and journal/payload finalization failures before human acceptance. Explicitly declare coordinator-only credential ownership for cache calls. Exclude internal LLM request/response wrappers from logical payload depth/item budgets consistently during capture and journal validation; retain ordinary workflow limits, credential scrubbing and authoritative payload identity. Qualify both cache calls and nested graph payloads through shared transport and reporting.
- [ ] Record human acceptance.

The cache and workflow-capture repairs pass 23,644 unit cases at 100% line/branch coverage, causal rejection controls and independent review. Both selected live cases passed with successful Allure publication; saved graph journals hydrate against authoritative payloads. Human acceptance remains open.
