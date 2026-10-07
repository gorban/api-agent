# README and usage documentation

## Checklist

- [ ] Start `README.md` with application and TUI usage documentation, deriving configuration details from the declarative executable descriptions.
- [ ] Document equivalent environment/CLI names, `.env` < environment < CLI precedence, strict `True`/`False` boolean values, bare boolean flags, and both `--key=value` and `--key value` forms.
- [ ] Add the TUI parser caveat: values starting with `--` cannot be parsed as values and are treated as another option key.
- [ ] Document `--no-std-in`, aggregate missing-configuration errors, available `TUI_MODE` selections, and the mandatory human acceptance for E2E runs.
- [ ] Document the [pre-TUI worktree provenance gate](implementation_plan_a6.md), its base/selected-pack repository checks, commit-versus-nonignored-dirty-file recency rule, timestamp limitations for deletions, and selected/newest worktree display. Explicitly document the stale-checkout stdin-acknowledgement exception to `NO_STD_IN`, help, and collect-only: there is no bypass setting; decline/EOF/unavailable stdin or an unverifiable required checkout exits nonzero before an operation starts.

- [ ] Generate documentation for shared/pack default hierarchy, conditional required settings, selectable prerequisite combinations, local database seeding, and host UI startup/path/app overrides from the [infrastructure declarations](implementation_plan_h.md).

- [ ] Document the All/tag/file/individual test picker, positional selectors with explicitly configured test mode, and default all-tests behavior. Document exit statuses, fixed unit coverage enforcement, integration without reruns, and E2E-only consecutive confirmation reruns; only unit execution may bypass the picker. Integration/E2E always show the selector-filtered picker and require explicit Continue; `NO_STD_IN` is invalid for their execution. E2E also requires its human-acceptance warning/acceptance. Help and collect-only retain their noninteractive exceptions.

- [ ] Document interactive local-run `b` browser destinations and `l` log-source menus, numbered shortcuts, `l` then `*` for source-labeled interleaved all logs, conditional entries, and named `RUN_LOG_SOURCE` configuration: default `agent` interactively, `all` under `NO_STD_IN`; explicit values choose the initial source. Document labeled interleaved output versus raw single-source output, and clarify that numeric menu shortcuts are not config values. Generate actual endpoint/configuration descriptions from runtime declarations.

- [ ] Include `-h` / `--help` / `HELP=True` and `--collect-only` in generated usage documentation. Show configured-mode, positional-selector-filtered collection examples, including environment-provided `TUI_MODE` with `NO_STD_IN=False` that still lists tests without further stdin after the worktree provenance gate. Explain that collection needs no inference credentials or E2E acceptance and creates no executed-test results.

- [ ] Document the interactive `.env` cleanup mode and unattended-auth setup suggestion/`s` skip, compact JSON profile format, optional secondary role, exact Keeper/1Password retrieval commands, and the requirement to log into the selected password-manager CLI before noninteractive retrieval.

- [ ] Document blank `.env` presence cells for absent values, checkmarks for present values, and `X` only for pending deletion. Keep unattended JSON out of general config cleanup; document its dedicated `x` clear/confirm action returning to attended authentication.

- [ ] Document Playwright E2E evidence, configurable `E2E_BROWSER_RESOLUTION=1440x900`, and headless default. Explain the `d` subset-only Debug start and optional `E2E_DEBUG`, its silent default, retained headed browsers, and explicit debug-session cleanup.

- [ ] Document provider-required test-only unattended browser login, implementation-time Playwright MCP walkthrough, conditional sign-in methods/password-only completion checks, and all-tab current-state Playwright failure evidence. Local startup does not run this preflight; configured provider authentication can occur lazily on API requests, and packs may offer an explicit authenticated UI launcher.

- [ ] Document local-run's optional pack-owned authenticated UI launcher, on-demand LLM provider authentication through the public provider contract, attended fallback, and explicit `AUTH_BROWSER_DEBUG` browser retention. Explain that authenticated handoff retains the same browser context and startup does not run a test login preflight.

- [ ] Document role-to-array unattended configuration, zero-entry attended fallback, automatic single-entry selection, and one shared zero-based `UNATTENDED_USER_INDEX` for needed credential arrays. Show `<vault>/<user>` choices and session-only interactive selection; explain the `NO_STD_IN` ambiguity error. Explain default acknowledgement as a chance to override new settings or preserve point-in-time configuration, with debug/index exclusions from automatic default saving.

- [ ] After the "Ok" milestone, document shared `LOG_CONTENT=False`: logs/Langfuse/traces omit user/LLM/tool content unless explicitly enabled with `True`, including local-test opt-in examples. Preserve the distinction from user-visible responses and separately required Allure evidence; credentials remain excluded regardless of this flag. See [content logging](implementation_plan_j.md).
