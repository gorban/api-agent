# Bedrock Responses native continuation

[Runtime endpoints](implementation_plan_f4o.md)

Preserve the native assistant state needed to submit client tool results to a
stateless Responses conversation. Use the existing opaque `Message.continuation`
contract and existing provider/session/worker owners; add no second session system.
Legacy plaintext-only reasoning tool responses without encrypted state or native
message metadata retain bounded suppression with no continuation; encrypted state
combined with plaintext reasoning still fails rather than entering replay.

- [x] Accept bounded printable `encrypted_content` on official reasoning items.
  Validate IDs, status, summary/content unions and unknown fields. Reasoning text
  is never copied into opaque continuation. Final answers without tool calls do
  not retain continuation state.
- [x] When tool calls need native replay, retain the complete admitted reasoning,
  assistant-message and function-call rows, including ordering and metadata.
  Require encrypted reasoning with empty plaintext summary/content. Check native
  visible text and calls against the normalized assistant message; replay exactly
  once instead of duplicating normalized assistant fields before tool outputs.
- [x] Request `reasoning.encrypted_content` for stateless Responses. Streaming may
  receive missing or incomplete cipher state at item-added; admit only validated
  cipher changes at item-done, preserving all other identity, content, part,
  status and final-snapshot equality checks. Reject malformed terminal state and
  retain observed usage without retrying a completed decoder failure.
- [x] Seal replay state through the existing provider owner, bound to native state,
  exact model, Responses API, endpoint, resolved region and immutable selected
  authentication snapshot. Verify before dispatch. A recreated provider or
  credential rotation invalidates state. Unresolved default-chain authentication
  or region cannot issue or replay state; fail explicitly without hidden identity
  probes or alternate credential acquisition.
- [x] Exclude opaque continuation, encrypted reasoning and binding material from
  every log, trace and report, independently of content flags. Keep credential
  guards and original evidence admission; the continuation is transport state.
- [x] Prove buffered and streaming tool-result round trips, visible-state matching,
  unknown-field and malformed-cipher rejection, cross-scope replay rejection,
  metadata equality, usage retention and absence from reports using synthetic
  fixtures. Run meaningful causal controls and independent source review before
  full source/artifact qualification. Real Runtime and human acceptance remain
  separately authorized checks.

Public interface sources: the maintained OpenAI SDK `ResponseReasoningItem` and
`ResponseOutputMessage` contracts; [AWS Responses API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html).

Offline qualification passes 22,464 units with exact 100% line and branch coverage, source distributions, installed/relocated packages, no-Git resources and independent/external source reviews.

- [ ] Human acceptance: complete text and tool-result turns on an authorized Runtime account with the selected models; inspect content-enabled reporting without opaque state.
