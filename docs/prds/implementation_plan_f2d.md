# Responses native message metadata and continuation

The included GPT provider must accept documented optional native metadata without rejecting an otherwise completed tool response after inference. This repair applies to tool-response continuation and preserves strict encrypted reasoning, tool-call correlation, declared schemas, usage, execution status and content-report privacy. Ordinary text-history phase preservation remains outside this continuation repair.

## Requirements

- [x] Accept missing, null and empty `output_text.logprobs`, and bounded well-formed entries with token text, byte integers, finite numeric log probability and typed top-token alternatives. Reject unknown keys, wrong types, nonfinite values and oversized metadata. Preserve supported metadata in owned opaque continuation.
- [x] Accept missing/null assistant message phase and the documented `commentary`/`final_answer` values. Preserve the native phase on follow-up replay; reject other values/types. Phase-bearing tool output needs native replay even when no reasoning item accompanies it.
- [x] Keep native item order and IDs, encrypted reasoning only, assistant text/calls and correlated tool results. Do not expose opaque continuation through enabled content export or accept malformed state as a compatibility fallback.
- [x] Use the same validation for API-key JSON and subscription SSE. Valid completed paid responses retain usage/outcome. Invalid metadata and conflicting terminal stream items retain available usage and potentially-executed status; do not automatically replay inference or change credentials.

- [x] Strict structured-output and tool schemas interpret reference keywords only at executable schema positions (root, property schema values and array items). Reference-like property names and enum instance JSON retain ordinary data meaning. Actual references and excluded composition remain rejected before transport; validation never retrieves remote resources.

## Acceptance

- [x] Synthetic HTTP JSON/SSE proofs cover reasoning, commentary, valid logprobs, function calls, tool results, wire round-trip and subsequent native replay.
- [x] Negative metadata shape/type/enum/bound cases and terminal phase conflicts reject; otherwise-valid mutation controls detect removal of their intended guards.
- [x] Focused provider checks and guard-mutation proofs pass; [synthetic regression coverage](../../tests/unit/test_responses_native_metadata.py) exercises the metadata contract.
- [x] Independent review and the repository gate complete.
- [x] A separately authorized fresh qualification after source freeze replaces the historical baseline; see [qualification evidence](implementation_plan_b2b1a.md).

## Public contract evidence

The [official output-text SDK schema](https://developers.openai.com/api/reference/python/__sdk_schema?declaration=(resource)+responses+%3E+(model)+response_output_text+%3E+(schema)&selected=(resource)+responses) defines optional logprob entries and their token, byte, probability and alternative fields. The [official output-message SDK schema](https://developers.openai.com/api/reference/python/__sdk_schema?declaration=(resource)+responses+%3E+(model)+response_output_message+%3E+(schema)&selected=(resource)+responses) defines optional commentary/final-answer phase and requires retaining it on assistant replay.

Implementation bounds are resource controls rather than claims about upstream maximums: at most 1,024 logprob entries, 20 alternatives per entry and 4,096 characters/bytes per token field; the existing bounded opaque-envelope and HTTP response limits still apply. No live provider call is needed for these deterministic proofs.

[Synthetic native metadata and replay proofs](../../tests/unit/test_responses_native_metadata.py) exercise both transports, opaque wire round-trips, subsequent requests and bounded rejection controls.

The metadata repair checkpoint passed 6,144 cases and 27 subtests with exact 100% line/branch coverage; the subsequent partitioned-credential repair records its current gate in [F2e](implementation_plan_f2e.md). Independent metadata/phase rejection mutations detect the intended guards. Installed-package continuation and privacy checks pass.

- [x] Complete [partitioned credential protection](implementation_plan_f2e.md), including independent review and the repository gate.


## Strict schema data verification

[Rich request regressions](../../tests/unit/test_rich_inference.py) parameterize reference-like property names and enum data through tool/structured request wire round-trips and native request mapping. Root/property/items reference controls reject before transport or retrieval. Controlled real loopback HTTPS and setup IPC verify installed structured results, tool arguments and continuation without external requests. Statistical source fingerprints must be refreshed after changes to this module; prior measurements remain historical.
