# Feature classification and defect categories

[Testing and reporting](implementation_plan_d.md) · [Staged checks](implementation_plan_i1.md)

Allure Categories classifies defect outcomes; an all-passing selected launch can correctly have no categories even when earlier history includes failures. Explicit feature labels populate Behaviors for every outcome; Suites retains repository/mode/file ownership. Do not classify passing tests as defects merely to fill Categories.

## Delivery

- [x] Declare explicit pytest `feature("Readable capability")` markers on every existing base and included-provider integration test. Support module/class/function inheritance and multiple features. Each marker requires exactly one bounded, nonempty printable string and no keyword arguments. Preserve ordinary tag selection and parameterized identities.
- [x] Collect values without executing tests or authenticating. Carry features on immutable TestIdentity into frozen previews/final Allure. Apply credential scrubbing to labels; preserve history IDs, attempt exclusions, statuses and suite labels. Legacy contributions without metadata remain readable until the separately planned static gate. Do not infer features from filenames.
- [x] Verify native generated Behaviors for passed/failed tests, empty Categories for an all-passing launch and default defect groups for failed/broken outcomes. Preserve historical immutable artifacts. Changed test-source fingerprints require compatible new token history; do not relabel old accounting from current source.
- [ ] Verify inherited/parameterized/provider collection, malformed markers, safe emission, stable grouping and applicable exact coverage, wheel/installed/native output and independent review.

## Later staged and CI enforcement

- [ ] Reject any integration/E2E test lacking explicit statically provable feature classification in the complete staged snapshot and clean-snapshot CI equivalent, including selected provider and pack suites. Cover inheritance, parametrization, additions/renames and invalid/dynamic declarations. An arbitrary requirement marker or filename is not classification. Report owner/test identity and an actionable fix.
- [ ] Analyze staged source/configuration without importing tests, collecting pytest or starting providers/services. Unstaged metadata cannot repair missing staged declarations. Preserve no-Git CI parity and meaningful marker-removal/invalid-marker mutations. Do not claim enforcement before its static-analysis task is implemented.

Use documented Allure feature labels and native defect categories. The custom recorder requires no Allure pytest runtime dependency.
