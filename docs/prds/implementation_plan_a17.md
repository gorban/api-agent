# Positional test groups and exclusions

[Configuration and TUI](implementation_plan_a.md)

Status: implemented; automated acceptance and source review passed. Human terminal/TUI review is pending.

## Selection contract

Apply the same selector behavior to `--tui-mode unit`, `integration` and `e2e`.
Accept individual tests, files, repository groups and tag groups displayed by the interactive test
picker. The `All tests` group is outside this enhancement. Preserve existing
repository-qualified IDs, directory/class/function selectors and quoted globs.

For example:

```sh
./launch.sh --tui-mode e2e sample-pack::tests/test_examples.py mutation -skip
./launch.sh --tui-mode unit tests/test_settings.py tests/test_settings.py::test_empty
```

- Positive selectors form a union. A test matched multiple times runs once.
- Prefix a selector with one `-` to exclude its matches. Support tags, files and
  individual tests and repository groups this way, including qualified forms. Exclusions always win,
  even over an explicit included test ID, irrespective of argument order.
- With only exclusions, start from the normal mode/owner default selection.
  Preserve the unit owner-scope and coverage rules in
  [owner selection](implementation_plan_a15.md).
- An empty effective selection must produce a clear no-tests diagnostic and
  must never fall back to running the default or entire suite.
- Bare tag names such as `skip` and `mutation` match exact tag membership.
  Preserve existing `tag:<name>` and repository-qualified tag forms. Reuse the
  picker catalog and group membership rather than maintaining a second list.
  Resolve ambiguous bare selectors with an actionable qualification diagnostic;
  retain qualified forms that distinguish repository, file, tag and test namespaces.
- Reject unknown, empty or malformed includes and exclusions before provider
  authentication, prerequisite acquisition or test execution. Do not silently
  ignore a misspelled exclusion. Existing cross-repository ambiguity protections
  remain in force.

## Command-line parsing and execution boundaries

Negative selectors must work without requiring the `--` separator in these test
modes. Continue supporting `--` for positional arguments. Before `--`, keep `-h`
reserved for help. After `--`, option parsing has ended: `-h` excludes the `h`
group and must not display help. This makes both meanings unambiguous on their
respective sides of the separator. Known long options and their typed values retain their
meaning, including negative numeric values consumed by an option. Unknown long
options must remain errors rather than becoming test selectors. Other launcher
modes must not acquire new single-dash semantics.

Use one selection resolver for terminal and interactive paths. Revalidation at
start must retain both includes and exclusions: catalog refresh must not restore
an explicitly excluded test. Preview/collection output should identify the
effective tests and exclusions without executing them.

Selection does not grant inference consent, bypass skip/skipif, authorize writes,
alter eligible-suite policy, change model profiles, or change confirmation/rerun
semantics. Including the `mutation` group does not bypass mutation approval.
File/tag expansion is not explicit enumeration of individual test IDs for the
[DEBUG acknowledgement policy](implementation_plan_a3.md).

## Acceptance

- [x] Prove red-to-green coverage for overlapping file/tag/test includes,
  duplicates, order-independent exclusions, and an explicitly included test
  removed by `-skip` or `-mutation`.
- [x] Cover exclusions alone, no matches, an empty final selection, qualified
  exclusions, namespace collisions, existing globs and repository ambiguity.
- [x] Cover negative selectors with and without `--`, `-h` as help before `--`
  and as an exclusion of tag `h` after `--`, ordinary
  long options, unknown options and negative typed option values. Assert that
  rejected input never starts authentication or prerequisites.
- [x] Prove the same selection algebra in all three modes, normal owner defaults,
  picker/catalog refresh, and unchanged skip, consent and DEBUG boundaries.
- [x] Run the applicable unit/coverage gates, dedicated adversarial review and
  external PR-style review before human review of terminal and TUI parity.

This task changes selection only. It does not authorize a live inference run or
modify application flows, renderer provenance, packaging or CI pipelines.
