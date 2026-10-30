# Response-bound credential-tail fixture

[Unit-suite performance investigation](implementation_plan_a9i.md)

The approved first slow-tail repair is limited to the four alternate-control
credential-carrier cases at the real response bound. Read the ordinary prefix
without artificial 127-byte fragmentation, stop exactly at the credential tail,
and fragment that tail at 127 bytes. Prove actual tail fragmentation, crossing
the unchanged 8 MiB guard, credential redaction and completed process/pipe
cleanup. Compare matched standalone coverage runs before and after, and reject
a mutation removing the relevant protection. Other slow families require their
own measured proposal and approval.

- [x] Retain the four-case behavior matrix and prove the ordinary prefix stops
  exactly at the tail boundary before fragmented reads begin.
- [x] Preserve real response-bound rejection and incomplete-credential redaction;
  reject a causal mutation disabling incomplete registered-range recognition.
- [x] Retain matched standalone coverage measurements and independently review
  the changed test. Require the applicable exact coverage and public-boundary
  gates before committing; full-suite savings remain unproven.

The matched standalone serial coverage runs passed four cases in 124.00 seconds
before and 120.85 after. This single pair shows a modest observed difference;
whole-suite benefit remains unproven. Prerequisite execution coverage was
unchanged, and the incomplete-range mutation failed at the leak assertion.

Human acceptance of the delivered recent unit repairs and noninteractive full-suite performance is recorded. Broader optimization and interactive picker behavior remain separate.
