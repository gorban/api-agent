# README and usage documentation

## Checklist

- [ ] Start `README.md` with application and TUI usage documentation, deriving configuration details from the declarative executable descriptions.
- [ ] Document equivalent environment/CLI names, `.env` < environment < CLI precedence, strict `True`/`False` boolean values, bare boolean flags, and both `--key=value` and `--key value` forms.
- [ ] Add the TUI parser caveat: values starting with `--` cannot be parsed as values and are treated as another option key.
- [ ] Document `--no-std-in`, aggregate missing-configuration errors, available `TUI_MODE` selections, and the mandatory human acceptance for E2E runs.

- [ ] Generate documentation for shared/pack default hierarchy, conditional required settings, selectable prerequisite combinations, local database seeding, and host UI startup/path/app overrides from the [infrastructure declarations](implementation_plan_h.md).

- [ ] Document the All/tag/file/individual test picker, positional selectors with explicitly configured test mode, and default all-tests behavior. Document exit statuses, fixed unit coverage enforcement, integration without reruns, and E2E-only consecutive confirmation reruns; only unit execution may bypass the picker. Integration/E2E always show the selector-filtered picker and require explicit Continue; `NO_STD_IN` is invalid for their execution. E2E also requires its human-acceptance warning/acceptance. Help and collect-only retain their noninteractive exceptions.

- [ ] Document interactive local-run `b` browser destinations and `l` log-source menus, numbered shortcuts, `l` then `*` for source-labeled interleaved all logs, conditional entries, and named `RUN_LOG_SOURCE` configuration: default `agent` interactively, `all` under `NO_STD_IN`; explicit values choose the initial source. Document labeled interleaved output versus raw single-source output, and clarify that numeric menu shortcuts are not config values. Generate actual endpoint/configuration descriptions from runtime declarations.

- [ ] Include `-h` / `--help` / `HELP=True` and `--collect-only` in generated usage documentation. Show configured-mode, positional-selector-filtered collection examples, including environment-provided `TUI_MODE` with `NO_STD_IN=False` that still lists tests without stdin. Explain that collection needs no inference credentials or E2E acceptance and creates no executed-test results.
