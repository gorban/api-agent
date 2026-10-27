# Markdown workflow proof of concept

The runtime loads versioned nested declarations from a selected `flow.md.html`,
captures their resources, and compiles registered LangGraph execution. The
[declaration grammar](prds/implementation_plan_b1b2.md) defines the inert source
format and resource bounds. The root example is a single chat component with one LLM call;
the permanent synthetic qualification fixture remains separate under
`demo/contrived/`. Layout v1 remains for focused compatibility units. Each of two components owns
its producer, format check, guard and two-pass budget. The model selects the
first route and readiness; deterministic code never manufactures those choices.
The selected bundled pack uses the separate [verified API demonstration](api_demo.md).

Requirements and acceptance gates are in
[the B2 hierarchy](prds/implementation_plan_b2.md).

Open root `flow.md.html` directly for its saved static declaration/diagram, or
use `./launch.py --view-flow` for checked managed viewing. See
[diagram maintenance and inspection](workflow_viewer.md) for markup-based
freshness, explicit generation and pre-commit checks.

## Local verification

Use a Python environment installed with the project's development dependencies.
Run focused deterministic tests before any live authentication or inference:

`python -m api_agent.workflows validate` checks the root one-call example.
`python -m api_agent.workflows validate --resource-pack-dir demo` checks the
selected bundled API demonstration.
`python -m api_agent.workflows validate --demo contrived` checks the separate
qualification fixture. Add `--base-root PATH` to select either from another
checkout. For an external pack, use `validate --base-root PATH
--resource-pack-dir PACK` (or `diagram` in place of `validate`) to apply that
pack's declared graph and ordered replacements. `--entry PATH` is a raw
declaration check; it does not read a pack manifest or apply replacements.
The CLI validates and projects declarations without provider setup or graph
execution.

```sh
python -m pytest tests/unit/test_workflow_contrived.py tests/unit/test_workflow_sampling.py tests/unit/test_workflow_evidence.py --cov=api_agent.workflows.contrived --cov=api_agent.workflows.sampling --cov=api_agent.workflows.evidence --cov-branch --cov-fail-under=100
```

Complete the combined applicable 100% unit gate, installed-artifact checks and
independent review before selecting live integration tests. Live tests require
the standard integration provider/authentication configuration documented by the
repository and may incur provider usage.

## Deterministic workflow coverage

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
independently of the selected pack's main workflow. Normal coverage requires no
statistical baseline, calibration refresh, skip or deselection. Historical pilots
and the explicit [maintenance procedure](workflow_calibration.md) remain separate.

Select normal coverage:

```text
tests/integration/test_workflow.py::test_workflow_node_coverage
```

Coverage draws fresh canonical version-4 UUIDs and uses the native fixture's
route/readiness decoder to discard combinations already exercised. It never
edits UUID bits. The ten reachable cases are two `skip` cases and four readiness
combinations for each of `next_a` and `next_b`. Rejected duplicate draws make no
LLM call. Each accepted UUID reaches the real graph unchanged, including its
prompts; the actual structured response, nodes, edges and required correction
attempts must pass validation before that case counts. Normal execution needs at
least 27 real completions; legitimate format repairs remain visible and costed.

`LLM_WORKFLOW_TEST_TIMEOUT_SECONDS` defaults to 600 seconds and covers provider
setup, catalog access, UUID selection and all ten graphs. Shorter parent deadlines
remain authoritative; existing owned cleanup drains separately. Provider profiles
may set the optional `LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS` cap. Application settings,
credential identity and ordinary model admission retain their existing behavior.

Structural evidence records actual attempts, guards, nodes, durations and routes.
Exact captured-resource hashes and owning commits remain in GRAPH journals for
historical joins. Additive declaration/settings digests are scoped diagnostics;
they do not replace exact source admission or prove arbitrary native/provider
behavior equivalent. See [identity requirements](prds/implementation_plan_b1c3a.md).
No live acceptance is established by deterministic unit tests alone.

## Controlled preference proof

The separate deterministic integration selection requires no provider fixtures:

```text
tests/integration/test_workflow_preferences.py::test_final_pass_relaxes_only_preference
tests/integration/test_workflow_preferences.py::test_truth_failure_stays_blocking_on_final_pass
```

Its checked-in `tests/integration/fixtures/workflow_preferences/flow.md.html`
hierarchy compiles through the actual declaration loader and workflow runtime and
matches registered function source files. Preference failure relaxes only on the
final pass; persistent truth failure exhausts the component. These tests publish
structural evidence. The permanent contrived model fixture retains strict guards
on every pass. Unit replay is `tests/unit/test_controlled_workflow_preferences.py`.

## Typed execution timeouts

The permanent qualification layout links graph, component and node timeouts to
`demo/cfg/config.toml`. `WORKFLOW_LLM_TIMEOUT_SECONDS` defaults to 120 seconds;
`WORKFLOW_GRAPH_TIMEOUT_SECONDS`, `WORKFLOW_COMPONENT_TIMEOUT_SECONDS` and
`WORKFLOW_NODE_TIMEOUT_SECONDS` remain unset by default. Each accepts a positive
finite float through ordinary configuration, environment or command-line options.
These optional settings do not add default acknowledgement prompts.

The selected API demonstration instead links its graph/component deadlines to
`demo/simple/cfg/config.toml`: 600 seconds for the graph and 300 seconds for a
component across its passes. Its LLM timeout is 120 seconds, and the guarded
component defaults to two passes. Child execution is clipped to the enclosing
deadline in both layouts.

A component deadline covers all of its passes. Every child budget is clipped to
the earliest enclosing deadline. The producer checks its LLM budget after the
call returns, so a provider swallowing cancellation cannot produce success after
its own timeout. Coverage records the resolved workflow budget identity; legacy
maintenance includes resolved settings in its historical sampling fingerprint. Unit tests cover typed defaults/environment/CLI,
linked numeric declarations, nested clipping, invalid values before inference and
swallowed cancellation; removing the post-call deadline check fails its test.

Calibration fingerprints also record the running Python version and installed
`langgraph`, `langchain-core`, `jsonschema`, `markdown-it-py` and `httpx` versions.
An explicit legacy calibration refresh binds its current dependencies; normal
coverage does not require a pilot after dependency upgrades. These are public package versions, without environment paths.

## Installed declaration check

Validate the root example with `python -m api_agent.workflows validate`, or the
selected demo with `python -m api_agent.workflows validate --resource-pack-dir demo`;
`diagram` prints declaration-derived topology without importing native bindings.
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
