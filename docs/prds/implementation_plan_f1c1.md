# Catalog credential authority handoff

[Provider-owned credential protection](implementation_plan_f1c.md)

- [x] Credential-using Bedrock discovery joins its selected credentials and the operation authority before SDK dispatch. Preserve lazy authentication and the selected credential identity.
- [x] Reject catalog metadata containing any credential in the joined authority before process IPC and hold the complete caller, coordinator and provider credential union through final generated metadata release. Synchronize concurrent registration and drain catalog work on close. Registration failure or cancellation must prevent dispatch and release owned resources.
- [x] Verify with offline synthetic SDK processes, both content flags, selected and caller-owned credentials, failed and cancelled registration, close during final validation, and a causal control that disables only authority registration. No live provider or inference is required.
- [x] Independent review and recheck.
- [x] Complete the full applicable test and exact line and branch coverage gate.
- [x] Complete external review.
- [ ] Human acceptance.

The synthetic process tests exercise the existing authenticated local handoff. A control returning only worker credentials instead of registering them admits a caller-owned credential in catalog metadata; the registered path rejects it. Focused provider and process tests passed, including cancellation and the four content-flag combinations. The final release holds the joined lease through the caller handoff; concurrent registration waits until release. Independent review identified and verified repairs to the authority union, lease lifetime and close ownership.
