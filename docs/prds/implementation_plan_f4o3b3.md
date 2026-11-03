# OpenRouter GPT-OSS reasoning qualification

[OpenRouter follow-through](implementation_plan_f4o3b1.md)

- [x] Qualify exact OpenRouter IDs `openai/gpt-oss-20b` and `openai/gpt-oss-120b` from the public catalog and documented reasoning contract. Expose Low, Medium and High; mandatory reasoning excludes Off. Do not infer official OpenAI support or qualify suffix aliases.
- [x] Preserve parameter-support routing for both Responses and Completions; unsupported effort values fail before inference. Empty continues to use provider defaults.
- [x] Prove all three efforts with bounded live requests on both APIs, retaining outcomes and usage. Verify the retained Studio browser accepts a configured low effort without the unknown-support blocker.
- [x] Close applicable unit coverage, causal rejection, package and independent review gates.
- [ ] Human acceptance of the three effort choices in the retained Studio browser.

- [x] Extract native Responses plaintext separately from the answer, preferring an upstream summary when present. Preserve the exact returned native items in order for replay, including signatures/encrypted state, without displaying opaque metadata. Bound and validate known fields; admit ordinary tool arguments through a 4,096-carrier combined limit, stop collection at that limit and retain the existing 8 MiB matching-work bound; retain official-route rejection and protect combined plaintext credential carriers. Preserve semantic JSON credential checks across reasoning, answer and tool keys/values with bounded work.
- [x] Prove real native Responses display and two-call replay for both qualified IDs, including retained Studio request display.

Effort qualification does not by itself qualify native continuation replay, streaming or cache hits. Completions native display/replay remains in the parent. A successful native request alone cannot establish that an upstream effort changed reasoning intensity.

When JSON-shaped credentials are registered, incomplete JSON text fragments are
conservatively withheld before evidence capture: safe semantic reconstruction
across arbitrary fields cannot be established within the bounded check. Numeric
JSON credentials also withhold numeric carriers whose equivalent spellings could
span fields. A numeric-shaped credential can therefore make this native route
unavailable even for otherwise benign IDs containing digits; use suitably
complex credentials rather than weakening this protection. Complete unrelated JSON and ordinary large tool objects remain
admissible; exhausted protection budgets fail closed.
