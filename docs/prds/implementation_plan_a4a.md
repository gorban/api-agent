# Test-owned UI browser evidence

[Failure evidence requirements](implementation_plan_a4.md) · [Prepared UI sessions](implementation_plan_i2c.md)

## Scoped contract

- [x] Provide an explicitly enabled capture and immutable attachment-submission
  seam for test-owned UI contexts. Content disabled performs no enumeration,
  browser reads, real capture or worker-image acceptance. The recording parent
  attaches a fixed notice image for tests declared as UI-session consumers.
- [x] Visit accessible pages across supplied contexts before caller teardown.
  Isolate ordinary page/context/capture/submission failures and retain fixed
  unavailable metadata. Propagate cancellation. Never replace test/setup failure
  or completed inference with an ordinary evidence failure.
- [x] Require a trusted provider-owned original-pixel capture policy. That owner
  masks all form values and registered credentials and establishes page-specific
  safety before returning PNG bytes. Missing or unverifiable safety yields
  unavailable evidence. This is no generic arbitrary-page safety guarantee.
- [x] Use fixed context/page ordinals and phase labels, excluding raw URLs,
  titles, DOM and error messages. Bound enumeration, image sizes, aggregate
  pixels and individual capture time. Submit immutable title/bytes/MIME tuples.
- [x] Join success and setup/test failure capture to the current attempt's
  existing Allure results and immutable S3 history before UI teardown.
- [x] Verify the included trusted provider masking against controlled form values,
  registered-secret displays and split-node text using actual browser pixels.
- [x] Independently review the capture policy and recording join.

## Limits and remaining acceptance

The helper accepts at most 16 contexts and 32 pages total, with a fixed overflow
marker. Each image is at most 4 MiB, aggregate images at most 16 MiB, and each
policy call has a five-second shared component deadline. Submission is a caller-
owned bounded synchronous callback. It must enqueue into the existing attempt
pipeline without blocking. The policy returns current whole-viewport evidence;
normal response preparation or additional full-page evidence remains caller-owned.

Only explicit test-owned UI contexts qualify. LLM-provider authentication
browsers, local-run consumers, private flows and full application E2E are outside
this delivery. No live authentication, inference, publication or human acceptance
is established by controlled unit checks.

## Optional provider capability

UI capability version 1 may advertise `capture_evidence`, implemented by
`async acapture_evidence(*, page, boundary) -> bytes | None`. Loading validates
the async hook and keyword signature. Consumers adapt this keyword hook to the
capture helper's `(page, boundary)` callback. The provider retains responsibility
for original-pixel safety, including all form values and live registered secrets;
return None whenever safety cannot be established. Advertising this capability
does not make every page safe or permit capturing authentication-provider contexts.

The included Studio policy binds capture to its successfully launched listener
and root document. It rejects frames, active requests and unstable document
content, masks editable controls and unsupported rendering surfaces, compares
transient text/attributes against the live secret boundary and verifies document
identity/content after capture. Controlled Chromium pixel tests retain ordinary
answer text while masking forms. Whole, split or overlapping registered secrets
in ordinary page content make pixels unavailable rather than relying on partial
mask localization. A final live-boundary check follows awaited marker cleanup;
submission occurs synchronously before timeout-progress teardown can yield.
Controls cover registration during capture/cleanup and progress-scope exit.

- [x] Complete exact applicable coverage, installed and public-boundary gates.
- [ ] Human review original saved screenshots with content enabled; confirm
  only the fixed notice with content disabled and unavailable evidence without a safe policy.

Controlled normal-runner checks preserve original PNGs in the matching successful,
setup-error and failed attempts, with immutable manifest hashes and byte-identical
secondary copies. Recording stays best effort on capture errors. DOM export is
bounded before crossing the browser boundary, and temporary markers are removed
on rejection. These checks use synthetic local storage and no paid requests.

## Content-disabled notice

- [x] For every recorded UI-session test with LOG_CONTENT other than True, attach
  one fixed, independently authored notice PNG explaining confidential-data
  withholding and the LOG_CONTENT=True option. State that credential masking
  and provider safety checks still apply; enabling content is not a safety bypass.
- [x] Decide from the parent's selected-test declaration, never returned worker
  markers. Do not enumerate or inspect browsers or read worker image files to
  produce the notice. Preserve the disabled-content worker/parent gates. Apply
  the same behavior to passed, failed and setup-error results.
- [x] Package reviewed static pixels with the runtime. Use bounded, nonfollowing
  regular-file access and exact size/digest checks. Missing or damaged resources
  fall back to the same fixed explanatory text without exporting replacement
  bytes or failing the completed test. No new renderer dependency is required.
- [x] Verify parent/worker isolation, all outcomes, missing/altered/FIFO resources,
  immutable Allure/secondary bytes, relocated wheel and applicable exact coverage
  and boundary gates.
- [ ] Human-review the explanatory image in the viewer when wired.

Notice delivery passes 9,583 units and 27 subtests at 32 workers, with exact
100% measured lines and branches. Independent mutation controls preserve the
disabled worker-image gate, selected-test authority, asset digest and narrow
source/license boundary. Two actual selected-provider tests with content off
attach only the static notice; immutable local/object/secondary bytes match.
