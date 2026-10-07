# README and usage documentation

## Checklist

- [ ] Start `README.md` with application and TUI usage documentation, deriving configuration details from the declarative executable descriptions.
- [ ] Document equivalent environment/CLI names, `.env` < environment < CLI precedence, strict `True`/`False` boolean values, bare boolean flags, and both `--key=value` and `--key value` forms.
- [ ] Add the TUI parser caveat: values starting with `--` cannot be parsed as values and are treated as another option key.
- [ ] Document `--no-std-in`, aggregate missing-configuration errors, available `TUI_MODE` selections, and the mandatory human acceptance for E2E runs.
