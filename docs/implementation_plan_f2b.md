# Lazy setup and provider integration suites

Normal user operations run through launch.py. No separate connection command is a prerequisite. This extends the [subscription connection layer](implementation_plan_f2a.md) and [GPT provider](implementation_plan_f2.md); inference remains a later capability.

## Checklist

- [x] Add the optional async provider `asetup` contract and lazy session-owner integration for attended and unattended setup. Preserve opaque tokens, coalesced initialization, deadlines and retained cleanup ownership. Validate hooks before execution.
- [x] Load the bundled GPT authentication-only factory through normal configuration contributions and overrides. Declare subscription settings adjacent to the provider; subscription requires no password-manager role. Keep unsupported inference/API-key capabilities explicit.
- [x] Add a shared `test_llm_setup` integration using `llm_provider.ensure_ready()`. Requesting the fixture is inert; its first setup request resolves the selected provider and invokes the common contract. Each worker owns its loop and auth resources until teardown.
- [x] Run integration selection from launch.py through the recorded runner and shared results/stop controls. Offer attended Start and, with an explicit account index, unattended Start. Account overrides apply to the run without rewriting the saved index. Uncached unattended subscription setup fails with an actionable attended-consent message, without browser or stdin interaction.
- [x] Extend integration discovery with shared base tests, selected pack suites and selected-provider declarations. Refresh on effective provider changes. Use adjacent `cfg/tests.toml`, version 1 and `[tests] integration=[...]`; paths and imported test contents stay bounded to the provider. Deduplicate physical file/function/parameter identities across roots, preserving the first identity.
- [x] Keep generic assertions in the shared integration suite, provider-specific assertions with the provider, and pack-dependent assertions with the pack. Declare selection-dependent provider suites outside shared/pack suite roots, which are always collected.
- [x] Verify synthetic cached success and uncached unattended failure through actual pytest workers, both sequentially and in parallel. Record both in Allure. Unit tests remain outside Allure; distributed integration-test code is excluded from production coverage while actual provider/runtime code remains measured.
- [x] Finish full unit coverage, packaging and independent join review before committing this batch. The combined launcher passed 2,321/2,321 units with exact 100% per-file production lines and branches; both wheels build and relocated installed declarations/factory resolve correctly. Independent reviewers exercised cleanup, worker setup and discovery boundaries.
- [ ] Join launch-managed recording prerequisites before live setup acceptance: prepare report dependencies and acquire required local storage profiles before execution, release/retain them under the existing operation owner, and preserve cancellation cleanup. The current recorded integration adapter validates storage configuration and reports publication failures but does not acquire those prerequisites itself.
- [ ] Human-run the setup integration from launch.py and complete actual attended sign-in. Record eligibility/callback behavior and first live setup failure/success in Allure.
- [ ] Incrementally add supported unattended login-page automation, detecting interaction/consent challenges and reporting an actionable error rather than waiting for stdin. Test observed steps before extending them; no credential-type fallback.
- [ ] Add account listing, explicit selection/reconnect and sign-out to launch-managed setup when those interactions are needed. GPT_PROFILE currently supplies explicit saved-profile selection; ambiguous registrations do not silently select an account.
- [ ] Wire the completed setup session to actual discovery/inference/local-run consumers and backend-owned suite sessions. Authentication-only worker setup does not implement those consumers or claim one shared cross-process token owner.

## Configuration and discovery

GPT configuration is at [its executable declaration](../src/api_agent/llm_providers/gpt/cfg/config.toml). Empty GPT_STATE_DIR uses the protected per-user location; empty GPT_PROFILE selects exactly one existing registration or starts initial attended consent. Multiple saved registrations require an explicit profile. GPT subscription account selection is distinct from password-manager account-pair indexes.

Factory creation, help, collection and test selection do not authenticate. Integration tests explicitly request setup when needed. Recorded execution requires interactive Start and Allure storage configuration; collection-only does not start setup or storage. E2E execution and its mandatory human acceptance remain separate work.
