# Selected unattended key-mode qualification

[Bedrock requirements](implementation_plan_f4.md) · [Modes and transport](implementation_plan_f4d.md) · [Public workflow](implementation_plan_f4e.md)

Qualify the existing LLM request test through ordinary launch.py with explicitly selected password-manager users. Access-key mode reads the selected login username as access key ID and password as secret access key. API-key mode reads only the selected login password as the Mantle bearer key. Neither mode may fall back to a session profile, another user, another endpoint or a different API/model.

- [x] Verify selected-index field reads, explicit-region source isolation, maintained SDK signing/bearer handling and credential-safe failures with focused offline tests and independent review. Existing runtime implements these paths.
- [x] With explicit account readiness and inference approval, pass the existing integration LLM request once with access_key and once with api_key. Use Responses, us-east-1, openai.gpt-oss-20b and the existing maximum 256 output-token bound. Preserve configured model/API/index identity; require completed, nonempty inference rather than a catalog-only success or skipped test.
- [x] Retain only non-secret effective configuration, status, numeric/unavailable partitioned token usage and fixed error classifications. Disable persistent test reporting for this qualification. Do not retain raw provider messages, prompts, credentials or authentication output. Unknown returned-model provenance and prices stay unknown.
- [x] Complete independent documentation and final external review of the exact successful results before human acceptance. Both reviews approved the bounded results with no actionable findings.
- [ ] Obtain human acceptance of this bounded key-mode qualification.

Command-line qualification overrides must not overwrite saved settings. Missing password-manager access is an explicit setup blocker. These key modes do not establish unattended session login, federated MFA, key expiry/rotation, live streaming/tools or Run local acceptance. Full unit/coverage, packaging and public-boundary checks remain required for main adoption.

## Bounded live results

On October 25, 2026, both selected modes passed the existing integration LLM request through ordinary launch.py with the authorized settings above. Each test required completed, nonempty inference and exited successfully; persistent reporting was disabled. Command-line overrides did not save settings. The configured model is established; returned actual-model provenance and prices remain unknown.

| Mode | Selected index | Non-cached input | Non-reasoning output | Reasoning | Cache read | Cache write | Total tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| access_key | 0 | 72 | 10 | 36 | 0 | Unknown | 118 |
| api_key | 1 | 72 | 10 | 17 | 0 | Unknown | 99 |

Counts are partitioned terminal accounting; unknown cache writes remain unreported rather than being treated as zero. These passes complete the two bounded live tests, while human acceptance above remains open.
