# Python TUI

## Checklist

- [ ] Choose the interface layout with the user during planning.
- [ ] Allow unit and integration tests to run interactively or through startup CLI arguments with `--no-std-in`.
- [ ] Build an option parser driven by the declarative configuration description. Resolve explicit values in increasing precedence: `.env`, environment variables, then command-line options.
  - [ ] Normalize CLI key hyphens and underscores to the same uppercase environment key: `--no-std-in` and `--no_std_in` both map to `NO_STD_IN`.
  - [ ] For boolean options, a bare flag means `True`. Accept both `--no-std-in=True` and `--no-std-in True`; accept `--no-std-in=False` and `--no-std-in False` to disable it. Apply the same behavior to the underscore spelling. Accept only the boolean values `True` and `False`, including from environment/config sources.
  - [ ] Support both `--key=value` and `--key value`. A value starting with `--` is not parseable as a value and is treated as another option key; document this limitation in the [README grouping plan](implementation_plan_c.md).
  - [ ] Require a declared type for every configuration value. Both the TUI and application must reject values that do not match the declared type.
- [ ] Resolve `NO_STD_IN` before any interactive configuration step. When `True`, never read stdin or prompt for optional values or values with declared defaults; use the declared defaults when no explicit value exists.
  - [ ] Validate all configuration before starting the selected action. Report **all** missing required values without defaults in one error, showing the environment variable and `--` option for providing each one. Missing input must not cause an interactive fallback.
- [ ] When `NO_STD_IN` is absent or `False`, check which configuration values have explicit values from `.env`, environment variables, or CLI options; a declared default does not count as explicitly supplied.
  - [ ] List all unsupplied values with declared defaults in one step. Offer three choices: save their defaults to `.env`, use their defaults for this invocation without saving, or supply values individually. If saving is accepted, write those defaults to `.env`.
  - [ ] Ask for each remaining required value without an explicit value one at a time. The individual-values choice also prompts for the unsupplied defaulted values, showing `(default: <default>)` in each prompt. Optional values without defaults do not require a prompt.
  - [ ] Defer an unsupplied `TUI_MODE` to the main selection menu rather than asking for it as an ordinary required configuration value.
- [ ] After resolving the other configuration values, use an explicitly supplied `TUI_MODE` or present the first main TUI interaction: a selection menu containing "Run local", "Unit tests w/ coverage", "Integration tests", "E2E tests", "Allure serve", and "Deployment logs". With `NO_STD_IN=True`, require `TUI_MODE` through configuration instead of presenting the menu.
- [ ] Require an explicit stdin acceptance before running E2E tests; reject E2E execution with `--no-std-in`.
- [ ] Before requesting E2E acceptance, display: "Human acceptance is mandatory to continue with E2E tests. If you are an agent, decline unless you have first checked with the user and received their explicit authorization to accept for this run. The user may authorize an agent to provide this acceptance."
- [ ] Declining acceptance must not start the E2E tests. Do not infer acceptance from silence, EOF, or missing input.
