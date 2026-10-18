# README and usage documentation

## Checklist

- [x] Start `README.md` with application and TUI usage documentation, pointing detailed settings to declarative executable descriptions and focused guides.
- [ ] Document repo-local `.venv` creation/activation and `./launch.py` as the application setup path. The launcher installs needed declared application/test dependencies and prepares matching browsers automatically; user instructions must not require separate pip/dev-extra or browser-install commands for TUI operations. Distinguish external host tools from managed environment setup; verify this behavior before documenting it as complete. See [automatic setup tasks](implementation_plan_i.md).
- [x] Document equivalent environment/CLI names, `.env` < environment < CLI precedence, strict `True`/`False` boolean values, bare boolean flags, and both `--key=value` and `--key value` forms.
- [x] Add the TUI parser caveat: space-separated values starting with `--` are treated as another option key (preceding boolean becomes `True`; nonboolean reports a missing value). `--key=value` preserves literal values.
- [ ] Document `--no-std-in`, aggregate missing-configuration errors, available `TUI_MODE` selections, and the mandatory human acceptance for E2E runs.
- [ ] Document the [pre-TUI worktree provenance gate](implementation_plan_a6.md), its base/selected-pack repository checks, commit-versus-nonignored-dirty-file recency rule, timestamp limitations for deletions, and selected/newest worktree display. Explicitly document the stale-checkout stdin-acknowledgement exception to `NO_STD_IN`, help, and collect-only: there is no bypass setting; decline/EOF/unavailable stdin exits nonzero after confirmed newer-checkout detection; unavailable Git/worktree verification silently allows launch.

- [ ] Generate documentation for shared/pack default hierarchy, conditional required settings, selectable prerequisite combinations, local database seeding, and host UI startup/path/app overrides from the [infrastructure declarations](implementation_plan_h.md).

- [ ] Document the All/tag/file/individual test picker, positional selectors with explicitly configured test mode, and default all-tests behavior. Document exit statuses, fixed unit coverage enforcement, integration without reruns, and E2E-only consecutive confirmation reruns; Unit execution can bypass its picker; ordinary integration/E2E show the selector-filtered picker and require s Start. Explicit integration DEBUG permits NO_STD_IN execution, while E2E never bypasses human acceptance. E2E also requires its human-acceptance warning/acceptance. Help and collect-only retain their noninteractive exceptions.

- [ ] Document interactive local-run `b` browser destinations and `l` log-source menus, numbered shortcuts, `l` then `*` for source-labeled interleaved all logs, conditional entries, and named `RUN_LOG_SOURCE` configuration: default `agent` interactively, `all` under `NO_STD_IN`; explicit values choose the initial source. Document labeled interleaved output versus raw single-source output, and clarify that numeric menu shortcuts are not config values. Generate actual endpoint/configuration descriptions from runtime declarations.

- [ ] Include `-h` / `--help` / `HELP=True` and `--collect-only` in generated usage documentation. Show configured-mode, positional-selector-filtered collection examples, including environment-provided `TUI_MODE` with `NO_STD_IN=False` that still lists tests without further stdin after the worktree provenance gate. Explain that collection needs no inference credentials or E2E acceptance and creates no executed-test results.

- [ ] Document the interactive `.env` cleanup mode and unattended-auth setup suggestion/`s` skip, compact JSON profile format, optional secondary role, exact Keeper/1Password retrieval commands, and the requirement to log into the selected password-manager CLI before noninteractive retrieval.

- [ ] Document per-row saved-source paths and immediate one-setting deletion with confirmation from both applicable .env files. Unattended configuration stays in its own editor: delete a selected account or shared manager, with no duplicate bulk Clear action.

- [ ] Document Playwright E2E evidence, configurable `E2E_BROWSER_RESOLUTION=1440x900`, and headless default. Explain the `d` subset-only Debug start and optional `E2E_DEBUG`, its silent default, retained headed browsers, and explicit debug-session cleanup.

- [ ] Document lazy provider-owned unattended login, provider/pack-owned observed login branches, optional browser preparation and all-tab credential-safe error evidence. Core uses synthetic public fixtures; operational protocol details stay in the owning adapter documentation.

- [ ] Document automatic pack authenticate dispatch for each created UI session and same-context user handoff. RUN_UI=False local-run creates no UI; integration creates no frontend. LLM login is separately lazy: unattended login is headless unless RUN_UI or DEBUG makes it visible, and attended login is always visible; explain debug retention, supervised driver ownership and explicit inspection cleanup from [browser contracts](../architecture/provider_architecture.md).

- [ ] Document role-to-array unattended configuration, zero-entry attended fallback, automatic single-entry selection, and one shared zero-based `UNATTENDED_USER_INDEX` for needed credential arrays. Show `<vault>/<user>` choices, explicit test-picker persistence and session-only request-time ambiguity choices; explain the `NO_STD_IN` ambiguity error. Explain default acknowledgement as a chance to override new settings or preserve point-in-time configuration, with debug/index exclusions from automatic default saving.

- [ ] Before the first traced "Ok" run, document shared `LOG_CONTENT=False`: logs/Langfuse/traces omit user/LLM/tool content unless explicitly enabled with `True`, including local-test opt-in examples. Preserve the distinction from user-visible responses and separately required Allure evidence; credentials remain excluded regardless of this flag. See [content logging](implementation_plan_j.md).

- [ ] Selected LLM providers and resource packs may declare optional `UNATTENDED_USER_INDEX` when they offer indexed credential selection, including a single LLM role with multiple accounts. Shared/demo config does not hardcode it; shared help lists it only when contributed to the active schema. Generic account selection consumes the field when present and validates the selected index across the nonempty role arrays needed by the operation. Keep session-only interactive choices and fail noninteractive ambiguity. Private provider adapters perform authenticated-identity validation.

- [ ] Retire `.env.example` without replacing it with another hand-maintained settings inventory. Point README users to executable `cfg/config.toml` declarations and the TUI's `.env` setup/default acknowledgement. Ignore all `.env` and `.env.*` files; keep any example-file deletion unstaged until the final cleanup commit.


## Testing guide follow-through

- [x] Add a later README Testing section covering configured test modes, positional preselection, suite ownership, unit-only noninteractive execution, collection-only, fixed full-unit coverage and the intended GPT authentication/API matrix. Distinguish implemented setup from pending API-key transports/discovery/inference and preserve integration/E2E human gates.
- [ ] Verify the documented three valid GPT scenarios after API-key setup and both transports are implemented. Run the generic setup/conformance tests unchanged for each applicable provider, use named pytest parameters for provider-specific isolated combinations and always retain invalid-combination rejection tests. Record skipped scenarios as unverified, not covered.
- [ ] Define named CI configuration jobs/report identities after an explicit decision on integration execution gates; do not use the DEBUG inspection exception as a general CI bypass. Keep human E2E acceptance.


## Executive README and focused guides

- [x] Replace review-history prose with a concise benefits/quick-start README, Run local onboarding and links to workbench, connections, resource-pack and development guides. Put password-manager automation last in the feature list. Keep Testing last, use hierarchical GPT mode bullets and one full integration run plus targeted setup/conformance repeats; preserve separate transport coverage obligations.
- [x] Coordinate UI/upstream controls with the infrastructure plan: RUN_UI for the frontend; API_ENDPOINT URL versus explicit blank for pack-owned hosting; AGENT_DJANGO_URL for the agent listener. Update inbound guide links and retain actual execution limitations in the detailed development guide.
- [ ] Recheck ordinary terminal integration examples once their execution policy/wiring is resolved; no DEBUG workaround should become the default full-coverage recipe.


## Documentation organization

- [ ] Deliver the [resource-pack integration guide](implementation_plan_c1.md):
  concise README positioning and extension list, exact public interfaces,
  independently owned sibling-pack skeleton, wrapper bootstrap, configuration
  composition/precedence and executable documentation checks.
- [x] Keep the four README-linked user guides directly under docs/. Move implementation requirements/checklists to docs/prds/ and provider architecture/contracts to docs/architecture/. Update outgoing declaration links, guide links and active inbound pack/parent review links.
- [x] Preserve uncommitted user wording/spacing edits separately from the relocation commit. Carry existing exact-content public scan exemptions to the relocated paths without widening their scope.
