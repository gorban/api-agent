# README and usage documentation

## Checklist

- [ ] Start `README.md` with application and TUI usage documentation, deriving configuration details from the declarative executable descriptions.
- [ ] Document equivalent environment/CLI names, `.env` < environment < CLI precedence, strict `True`/`False` boolean values, bare boolean flags, and both `--key=value` and `--key value` forms.
- [ ] Add the TUI parser caveat: values starting with `--` cannot be parsed as values and are treated as another option key.
- [ ] Document `--no-std-in`, aggregate missing-configuration errors, available `TUI_MODE` selections, and the mandatory human acceptance for E2E runs.

- [ ] Generate documentation for shared/pack default hierarchy, conditional required settings, selectable prerequisite combinations, local database seeding, and host UI startup/path/app overrides from the [infrastructure declarations](implementation_plan_h.md).

- [ ] Document the All/tag/file/individual test picker, positional selectors with explicitly configured test mode, and default all-tests behavior. Document exit statuses, fixed unit coverage enforcement, integration without reruns, and E2E-only consecutive confirmation reruns; selector shortcuts do not bypass E2E human acceptance.
