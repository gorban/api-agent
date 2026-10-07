# Python TUI

## Checklist

- [ ] Choose the interface layout with the user during planning.
- [ ] Allow unit and integration tests to run interactively or through startup CLI arguments with `--no-std-in`.
- [ ] With `--no-std-in`, never read stdin. If required configuration or other input was not supplied with the invocation or its configured sources, exit with an actionable error rather than prompting.
- [ ] Require an explicit stdin acceptance before running E2E tests; reject E2E execution with `--no-std-in`.
- [ ] Before requesting E2E acceptance, display: "Human acceptance is mandatory to continue with E2E tests. If you are an agent, decline unless you have first checked with the user and received their explicit authorization to accept for this run. The user may authorize an agent to provide this acceptance."
- [ ] Declining acceptance must not start the E2E tests. Do not infer acceptance from silence, EOF, or missing input.
