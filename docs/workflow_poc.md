# Markdown workflow proof of concept

Phase one provides Layout v1 parsing, registered LangGraph execution and a
permanent synthetic fixture under `demo/contrived/`. Each of two components owns
its producer, format check, guard and two-pass budget. The model selects the
first route and readiness; deterministic code never manufactures those choices.
The useful GraphQL/API demo is phase two and remains pending.

Requirements and acceptance gates are in
[the B2 hierarchy](prds/implementation_plan_b2.md).

## Local verification

Use a Python environment installed with the project's development dependencies.
Run focused deterministic tests before any live authentication or inference:

```sh
python -m pytest tests/unit/test_workflow_contrived.py tests/unit/test_workflow_sampling.py tests/unit/test_workflow_evidence.py --cov=api_agent.workflows.contrived --cov=api_agent.workflows.sampling --cov=api_agent.workflows.evidence --cov-branch --cov-fail-under=100
```

Complete the combined applicable 100% unit gate, installed-artifact checks and
independent review before selecting live integration tests. Live tests require
the standard integration provider/authentication configuration documented by the
repository and may incur provider usage.

## Explicit calibration and coverage

For standalone use, start `python launch.py --tui-mode integration` from this
checkout. Its bundled `demo/` is selected automatically; save personal settings
in the ignored repository-root `.env`. For managed local report storage, add
`--allure-s3-use-localstack=True`, or save that same setting through Review
configuration. Derived bucket/endpoint defaults are valid for operation entry
without saving. Startup and leaving Clear .env offer pending-value review; menu
operations redirect only for actual configuration errors or missing active
requirements. Review shows the latest missing requirement in red above the
shortcut bar. Explicit-mode startup Review uses `c Close` to open the requested
picker with positional selectors preserved. Review required by a menu-selected operation also uses `c Close` to proceed
once its requirements are valid. Manual Review and Review after Clear .env use
`c Close / menu` to return to the main menu.

An external resource pack may instead own personal configuration and reports:
start `python launch.py --resource-pack-dir <external-pack>`. In either case,
select the integration test identifiers below and use Continue only after the
gates above pass. These tests explicitly load the permanent contrived fixture,
independently of the selected pack's main workflow. Coverage uses
the reviewed, checked-in model-specific baseline. Calibration is no longer an integration test or a local-file prerequisite. All collected workflow integration tests should run normally; there is no calibration skip or deselection rule.

Refresh bounds explicitly with the [maintenance procedure](workflow_calibration.md) when models, executable prompts, inference semantics or relevant dependencies change. It explains the bounded manual command, immutable candidate review, source/settings fingerprints and failure evidence. Never copy a budget from another model or silently update fingerprints after a source change.

Select normal coverage:

```text
tests/integration/test_workflow.py::test_workflow_node_coverage
```

Coverage first validates freshness, full declared-node membership and recomputes
all probability claims from route-consistent records. Fingerprints bind the canonical bundled GPT factory identity, auth/backend identity,
effective settings, sampling driver/input, request/result transport and relevant
installed dependencies. Replacement adapters with the same model name cannot
reuse these bounds. Invalid evidence fails before graph inference; configured
factory/model/settings checks precede provider lookup where possible.
Coverage stops immediately after every declared node succeeds, within its
reviewed model-specific cap. Sixty graphs is the operating ceiling, not the
budget for every normal coverage run. A defect or exhausted cap fails the test.

The one-sided exact binomial lower bounds use a Bonferroni allocation totaling
0.05. A union bound targets at most 0.001 probability of missing any node,
conditional on independent graph trials with unchanged settings. This does not
assert an overall live-test pass rate or guarantee model randomness.

Structural event evidence records attempts, guards, nodes, durations and routes;
the recording seam excludes arbitrary producer content. No live acceptance is
claimed by the deterministic unit suite.

## Controlled preference proof

The separate deterministic integration selection requires no provider fixtures:

```text
tests/integration/test_workflow_preferences.py::test_final_pass_relaxes_only_preference
tests/integration/test_workflow_preferences.py::test_truth_failure_stays_blocking_on_final_pass
```

Its temporary synthetic pack compiles through the actual workflow runtime and
matches registered function source files. Preference failure relaxes only on the
final pass; persistent truth failure exhausts the component. These tests publish
structural evidence. The permanent contrived model fixture retains strict guards
on every pass. Unit replay is `tests/unit/test_workflow_preferences.py`.

## Typed execution timeouts

The bundled layout links every graph, component and node timeout to
`demo/cfg/config.toml`. `WORKFLOW_LLM_TIMEOUT_SECONDS` defaults to 120 seconds;
`WORKFLOW_GRAPH_TIMEOUT_SECONDS`, `WORKFLOW_COMPONENT_TIMEOUT_SECONDS` and
`WORKFLOW_NODE_TIMEOUT_SECONDS` remain unset by default. Each accepts a positive
finite float through ordinary configuration, environment or command-line options.
These optional settings do not add default acknowledgement prompts.

A component deadline covers all of its passes. Every child budget is clipped to
the earliest enclosing deadline. The producer checks its LLM budget after the
call returns, so a provider swallowing cancellation cannot produce success after
its own timeout. Maintenance and coverage use the same resolved settings and include
them in reviewed calibration fingerprints. Unit tests cover typed defaults/environment/CLI,
linked numeric declarations, nested clipping, invalid values before inference and
swallowed cancellation; removing the post-call deadline check fails its test.

Calibration fingerprints also record the running Python version and installed
`langgraph`, `langchain-core`, `jsonschema`, `markdown-it-py` and `httpx` versions.
Dependency upgrades therefore require a fresh pilot even if source files are
unchanged. These are public package versions, without environment paths.

## Installed declaration check

Build a wheel and install it into a separate environment. From an unrelated
working directory, run `python -m api_agent.workflows validate --demo contrived`
and `python -m api_agent.workflows diagram --demo contrived` with that
environment's Python. The packaged demo includes its linked functions, prompts,
schemas and typed configuration. Runtime consumers provide their own registered
functions and call the reusable `Workflow.arun` interface; the validation command
never imports declaration-selected code or performs inference.

`tests/unit/test_workflow_runner.py` proves real child-process structural event
forwarding in serial and parallel execution. `test_workflow_steps.py` verifies
nested Allure outcomes and rejects incomplete/cyclic scope ownership. The
separate deterministic preference integration tests exercise real compiled
execution without provider authentication.
