# Qualify test configurations

[Test execution](development.md#testing) · [Provider test profiles](integration_test_profiles.md)

Before integration/E2E commands, save `S3_USE_LOCALSTACK=True` in Review
configuration or append `--s3-use-localstack=True` for managed local history
(the shared LocalStack S3 service; see [prerequisites](prerequisites.md#shared-s3-localstack)).
For external S3, choose False and supply `S3_ENDPOINT` and `ALLURE_S3_BUCKET`.
The former `ALLURE_S3_USE_LOCALSTACK`, `ALLURE_LOCALSTACK_PORT`, `ALLURE_S3_ENDPOINT`
and `ALLURE_S3_REGION` names now fail with an error naming the replacement.
Unit tests need no report storage; they start LocalStack only when a selected
test carries a marker the pack maps to `api_agent:shared_s3`
(`./launch.sh --tui-mode unit --no-std-in 'pack::tag:localstack'`), and release it
at the end of the run.

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
