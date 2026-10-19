# Nested-declaration fixture qualification

[Qualification history](implementation_plan_b2b1a.md) · [Nested format](implementation_plan_b1b2.md)

- [x] Run a fresh 20-graph pilot for the converted permanent fixture using GPT-6 Luna, subscription Responses, medium cache and low reasoning. Preserve every trial and failure; do not relabel an older source fingerprint or retain its cap without measurement.
- [x] Independently verify the finalized private receipt, all contiguous successful observations, current source/dependency/settings fingerprint and exact statistical calculation before exporting the unsigned public entry. Keep receipt keys and local evidence outside source.
- [x] Verify the installed baseline and run the ordinary required workflow-coverage integration through the launcher with actual inference and Allure publication. Stop as soon as every declared node is covered.
- [ ] Human acceptance of the new format and qualification checkpoint. Earlier phase-one acceptance remains valid for its recorded scope.

The fresh pilot completed 20 graphs and 46 inference calls. Routes were `skip: 5`, `next_a: 8`, `next_b: 7`; all first-stage nodes appeared in 20 trials and all second-stage nodes in 15. Fingerprint version 3 binds the captured declaration/resource closure and current sampling sources.

Independent simultaneous one-sided 95% lower bounds justify a minimum of 13 runs for the conditional 99.9% node-coverage target: the missing-node union bound is `0.00098724257507006`. The existing four-run margin gives cap 17, within the operating ceiling of 60, with buffered bound `0.00008371852331830926`. This does not claim live-system availability.

The reviewed unsigned entry replaces only this profile in `fixture_baselines.json`. The normal coverage integration passed, covering all six declared nodes in two graph runs, and Allure publication passed. Token usage was recorded; quota snapshots were unavailable, so no quota-based cost is asserted. Managed viewing, application E2E behavior and the separate provider-cache issue retain their own gates.
