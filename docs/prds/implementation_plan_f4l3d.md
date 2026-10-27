# Launcher test defaults and qualification

[Operation-scoped profiles](implementation_plan_f4l3.md)

Provider-dependent test controls belong in selected-case review so acknowledging
application defaults cannot accidentally pin another provider's test configuration.

- [x] Hide `*_TEST_*` provider test controls and
  `LLM_SETUP_REQUEST_TIMEOUT_SECONDS` from ordinary configuration Review,
  including provider declaration overrides. Keep application controls visible.
- [x] Offer selected-test configuration review for integration and E2E selections
  even when no tool-specific fields exist. Display exact case identity, effective
  values and source after provider profile defaults and explicit test overrides.
- [x] Keep blank global test models unpinned; show qualified model, effort,
  output cap and timeout defaults in the selected case's effective review.
- [x] Preserve explicit dotenv/environment/CLI/test overrides, including false
  and empty values; do not infer intent from a saved value or silently remove it.
  Keep application settings, credential identity and environment unchanged.
- [x] Verify both absent and explicit false reasoning-token-reporting overrides:
  absent uses the qualified profile, explicit false remains visible with its
  source and fails the unchanged pre-inference qualification guard.
- [x] Qualify the configured default SDK-profile authentication path independently
  of the explicit native-login adapter; preserve old observations and unrelated
  provider baselines. The fresh 60-graph pilot supports cap 50 with the unchanged
  confidence, coverage target and fixed margin.
- [x] Pass normal reasoning and workflow integration with the effective defaults.
- [x] Independent review and final applicable verification.
- [x] Record the user’s conditional acceptance of this launcher follow-up after
  technical closure; no additional manual launcher exercise is claimed.

The review is read-only. Application settings remain editable in the existing
Review flow; explicit test controls use dotenv, environment or CLI. Removing an
existing saved override requires an explicit user decision through cleanup.

The default SDK-profile pilot passed 60 graphs/160 calls: minimum 46 plus fixed
margin four gives cap 50. Fresh workflow integration passed four graphs/12 calls;
fresh reasoning passed all six calls with signed deltas 478, 1256 and 1257.
The earlier empty-answer reasoning failure remains a failure. Applicable units
passed 18,700 tests with exact coverage of 45,971 statements and 17,994 branches.
Final package, boundary and independent/external review passed. These results
supersede no earlier measurement's source, adapter or configuration identity.
