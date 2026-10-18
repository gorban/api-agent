# Stored cleanup receipt progress

[Complete credential scrubbing and cleanup causes](implementation_plan_a10b.md)

## Requirements

- [x] A stored cleanup receipt remains a usable retry capability when its original retry exports a replacement child duty until that child acknowledges. Subsequent retries of the same parent must advance known replacement duties before repeating a blocked parent callback; callers need not replace their saved handle to make progress. Direct child retry remains supported.
- [x] Preserve replacement versus independent attached-duty ownership, self receipts and cyclic receipt graphs. A child acknowledgement must not release independent outer duties. Preserve original interruption identity and arguments, and retain pending work for later retry.
- [x] Hold the real kernel suite lease through all retained duties and descriptor disposal acknowledgement. Retry and parent/child notification remain idempotent, with exactly-once disposal and lease release.

## Acceptance

- [x] Controlled stored-parent, multi-attempt child, direct-child, independent-duty, cycle and cancellation checks pass; real competing processes remain excluded until acknowledgement and enter afterward. Rescue every test-owned duty and file descriptor.
- [x] Focused mutations reject loss of child advancement, premature parent acknowledgement and duplicate cleanup. Applicable source coverage is complete for lines and branches.
- [ ] Complete independent adversarial review before joined validation.
- [ ] Complete joined unit/package gates and preserve any failed evidence. Human acceptance remains separate.
