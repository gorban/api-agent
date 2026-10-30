# Qualify test configurations

[Test execution](development.md#testing) · [Provider test profiles](integration_test_profiles.md)

Run all unit tests without selectors; require every test to pass and 100% line
and branch coverage. Run integrations under the documented qualified settings
needed to cover every applicable test. Providers can require different settings
for different tests; coverage does not require every possible setting combination.
For GPT, the valid discovery configurations are:

- `GPT_API_MODE`: `responses` or `completions`.
- `GPT_AUTH_MODE`: `api_key` or `subscription`; subscription requires Responses.

When one configuration qualifies the whole suite, run it once with those settings:

```bash
./launch.sh --tui-mode integration --no-std-in --start
```

Then repeat only the LLM discovery tests for the other valid combinations—two more
runs for GPT. For example, when this differs from your first run:

```bash
./launch.sh --tui-mode integration --no-std-in --start \
  --gpt-auth-mode=subscription --gpt-api-mode=responses \
  api-agent::tests/integration/test_llm_discovery.py
```

Discovery checks remote credential acceptance and catalog access without inference.
API-specific request tests also need coverage under each applicable mode. Repeat
affected tests after changing effective provider settings. A passing configured
run does not qualify other settings. The included Bedrock provider supplies
[operation-scoped test profiles](integration_test_profiles.md): ordinary
Responses calls and structured Completions workflows use separate defaults.
The application retains its own settings; configure the provider and supported
authentication once rather than switching API/model settings between tests.

Normal mandatory [workflow coverage](workflow_poc.md#deterministic-workflow-coverage)
uses the permanent deterministic route fixture and needs no statistical
calibration baseline. The small default chat graph is a separate example.
Historical [calibration maintenance](workflow_calibration.md) is not onboarding
or a prerequisite for normal test execution.
