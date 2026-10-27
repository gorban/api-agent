# Existing public inference test qualification

[Public launch workflows](implementation_plan_f4e.md) · [Bedrock requirements](implementation_plan_f4.md)

The existing `tests/integration/test_llm_request.py` must qualify the selected
provider through launch-owned setup, rather than a separate inference helper.

- [x] Bound the smoke request to 256 output tokens when the selected catalog
  declares output-limit support (including Bedrock), reduced to its advertised
  maximum when smaller than 256. Preserve providers without
  that capability, such as subscription sessions. Require a
  completed outcome and nonempty text; truncation, unavailable setup and skipped
  tests do not establish successful inference.
- [x] Preserve lazy `llm_provider` setup and `llm_selection` catalog/default
  selection. Shared setup owns authentication, cancellation and cleanup; the
  test does not acquire credentials or select a fallback provider.
- [x] Complete a real attended Bedrock request through this test: on 2026-10-25
  the user reports Ctrl+V works and the ordinary launcher test passes. The test
  requires a completed outcome and nonempty response. This is user-reported evidence.
- [ ] Retain selected factory, API mode and model from the successful resolved
  configuration alongside actual model and token-usage evidence. These details
  were not supplied with the pass report; no further inference is needed merely
  to inspect an existing result. Other auth/API/model combinations remain open.
- [x] Complete independent review and public-boundary checks before acceptance.
- [x] Report explicitly disabled Allure publication as skipped in the terminal;
  preserve the actual test result and exit status when authentication is
  unavailable or inference fails. Focused synthetic tests cover passed, failed
  and skipped test results independently of publication status.
- [x] Independently review the terminal publication-status correction.

## Running the existing test

From the repository root, open the ordinary attended integration flow:

```sh
./launch.py --tui-mode=integration --no-std-in=False --start=False --test-parallel-jobs=0 --skip-allure-reporting=True tests/integration/test_llm_request.py
```

Select the included Bedrock factory, intended authentication mode, region,
Responses or Chat Completions API and qualified model in configuration. Review
the selected account before accepting execution. `--start` starts the selected
operation after startup checks; use it only with authorization to infer.
`--skip-allure-reporting=True` uses the existing unrecorded path for this
proof of concept without requiring reporting services. This skips reporting
only; successful qualification still requires the selected test to report
**1 passed**, with no failed or skipped tests. Attended setup uses the launch interaction channel;
missing prerequisites remain unavailable/skipped and must be reported as such.

The test makes one request asking for `Ok`. For selections admitting an output limit, the cap bounds generated
tokens, not a monetary price or free-tier guarantee. Reasoning models may consume
the cap without returning text; that is a failed qualification, not permission
to retry or increase spending automatically.

## Offline evidence

The focused contract test exercises the existing integration function, verifies
one selected-provider call and the output bound against real public catalog
contracts with smaller, equal, larger and unspecified advertised limits, and rejects truncated/empty
responses. Existing recorded-inference tests exercise worker setup, both API
shapes, recording and credential-safe failure evidence with synthetic transport.
Live account acceptance remains separate.
