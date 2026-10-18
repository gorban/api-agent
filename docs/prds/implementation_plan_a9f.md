# Dependency-aware test domains — future proposal

[Unit execution and coverage](implementation_plan_a9.md)

This is unchecked future work, scheduled last after all profiling and remaining authentication tasks. It records requirements for evaluation; it does not implement a test split, alter execution defaults or change repository instructions.

## Ownership and dependencies

- [ ] Define explicit production and test ownership for configuration/contracts/resources; workflow parser/compiler/runtime; provider/auth/transport; API/session/stream; TUI; accounting/history/Allure; frontend/browser; and packaging/boundaries. Assign shared owners and cross-domain edges instead of assuming nested packages are isolated.
- [ ] Compute transitive impact through shared contracts, resources and lifecycle code. Include dynamic provider/configuration selection, test collection, dependencies, interpreter/runtime versions, worker settings, platform and source hashes in compatibility and invalidation inputs. Unknown, ambiguous or untracked dependencies require conservative invalidation, including the full suite when isolation cannot be established.
- [ ] Define the complete applicable measurement scope for each proposed domain and its shared dependencies. Require exact 100% applicable line and branch coverage and meaningful failure/mutation controls. Do not merge stale coverage across changed source/configuration/environment inputs or present selected-domain results as a complete joined result.

## Evaluation and delivery gates

- [ ] Benchmark the current runner against an isolated proposal before adoption: cold/warm collection and execution, domain-local and cross-domain changes, worker/runtime contention and artifact overhead. Use deliberate shared/dynamic dependency changes to prove invalidation correctness. Retain the existing approach if gains are immaterial or correctness/review quality declines.
- [ ] Preserve the maximum of three implementation lanes with independent review for each and explicit disjoint ownership. Integrate accepted slices before the serial external pull-request review and feedback loop; resolve actionable findings before proceeding. A domain split does not authorize parallel external reviews or reduced validation.
- [ ] Keep the full joined unit/coverage gate and applicable installed-package, platform, privacy and public-boundary checks. Independently review the ownership map, invalidation and benchmark evidence before deciding whether to implement. Implementation and human acceptance require their own future gates.

No implementation is claimed by these checkboxes. Package or test-file moves alone do not establish safe independence, and this proposal does not change current test selection or coverage reuse.
