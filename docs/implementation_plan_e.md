# Initial LangGraph and frontend API

## Checklist

- [ ] Establish [Allure reporting](implementation_plan_d.md) before the first integration or E2E test in this milestone, and persist results from the very first run to retain the implementation's development history. Exclude unit tests from Allure; require deterministic unit tests without live LLM calls and a 100% pass rate for every commit.
- [ ] Implement the [declarative configuration format and runtime parser](implementation_plan_b.md) as prerequisites for this milestone. Use it for application and TUI configuration, including required types, defaults/optional values, `.env`/environment/CLI precedence, and missing-value handling described in the [TUI plan](implementation_plan_a.md). Graph Layout construction remains the later extension.
- [ ] Implement authentication to the LLM provider for this milestone, including login/callback/session behavior. Cover the authenticated "Ok" API path in the example tests. Defer upstream API authentication to the resource pack, and that separate authentication is not required for the initial api-agent milestone.
- [ ] Create a minimal LangGraph with one LLM node instructed to say "Ok", using the UI-selected model and structured output with `response_text: "Ok"`. Defer reasoning-token streaming for this first milestone.
- [ ] Create the API immediately at the existing expected paths: `/app/agent/chat` and `/app/agent/chat/stream`. Preserve the request and response fields and stream lifecycle needed by the UI renderer to display the answer; keep any required envelope fields alongside `response_text` without implementing the legacy renderer workflow.
  - [ ] Cover the output schema including for terminal payload serialization and SSE events. Figure out how we'll compose routes for URLs.
  - [ ] Finalize consume contract: final `response_text` is displayed from the payload; streaming delivers a final structured payload through the `result` event. Inspect the required request fields and session/completion events before implementing the API.
- [ ] Add an example targeted integration test of the single-node graph and API response contract, reporting to Allure.
- [ ] Add an example E2E test showing "Ok" in the UI renderer through the API. Run it only through the [TUI human-acceptance workflow](implementation_plan_a.md); reject unattended `--no-std-in` execution and require the approved acceptance message.
- [ ] Complete this minimal milestone before extending graph construction to the [declarative Layout](implementation_plan_b.md).
