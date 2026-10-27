# Runtime-discovered configuration requirements

[Configuration review](implementation_plan_a9d.md) · [Provider configuration](implementation_plan_b.md)

Some setup requirements depend on an attempted operation, rather than static
configuration alone. Keep declaration loading, help and Review configuration
inert: they must not authenticate, probe remote services or execute providers to
decide whether a setting is required.

## Requirements

- [x] Allow a provider to return a canonical configuration-required error carrying
  only a bounded, unique tuple of setting names. Never include values, credentials
  or upstream diagnostics. Accept only fields declared in the effective schema.
- [x] In Run local, carry that requirement through the ordinary LLM operation and
  local-owner event to the configuration session. Scope facts to the effective
  operation snapshot; reject stale events and clear facts when other effective
  settings change. Allow requested values to be filled individually. Do not save
  validity flags or requirement facts in dotenv.
- [x] Display missing requirements as Required rows even when default
  acknowledgment is disabled or a saved string is empty/whitespace. Keep optional
  defaults excluded according to their declaration. Use the existing setting
  description and typed editor; persist only the values the user edits.
- [x] Offer Review configuration after an operation discovers missing values.
  Stop and settle the previous owner before leaving its screen. Filling the last
  field must not automatically restart provider work; require explicit forward
  continuation and use the new effective configuration on retry.
- [x] Offer an explicit Recheck setup action for Run local runtime requirements
  after external setup changes without configuration edits, including after
  returning through the main menu. Settle the old owner
  before clearing only the same scoped runtime facts; preserve static blockers,
  settings and dotenv. Make one fresh setup attempt, with no retry from editing
  or ordinary Close. Reject retired-owner events and stale recheck actions.
- [x] Preserve ordinary static conditional requirements, noninteractive error
  handling and provider-neutral configuration. Credential readiness and recovery
  policy belong to the selected provider, not the configuration resolver.

## Qualification and remaining scope

- [x] Independently verify the real error/event/session/TUI path, unknown and
  malformed names, stale snapshots, changed selectors/profile/index, partial
  edits, blank required strings, descriptions, persistence, cleanup failure,
  external readiness with unchanged settings, retired-owner events, stale reset
  tokens and explicit retry. Require focused coverage of added executable lines/branches
  and meaningful causal controls.
- [ ] Complete external review and human acceptance of this interaction. Keep
  full applicable unit tests and packaging qualification as integration gates.
- [ ] Extend runtime requirement delivery to integration-test subprocess owners
  or other consumers separately. This milestone connects Run local; a structured
  provider error alone does not establish another consumer's review navigation.

Provider-owned acquisition and routing conditions are specified in the
[Bedrock session requirements](implementation_plan_f4a2.md).

Interactive Run local authentication uses the shared masked transport under
[operation ownership requirements](implementation_plan_a9k1.md).
