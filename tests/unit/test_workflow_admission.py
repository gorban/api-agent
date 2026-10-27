"""The coordinator admits definitions independently of worker journals."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from api_agent.testing.workflow_admission import capture_workflow_admissions


def test_selected_definition_is_admitted_without_loading_native_functions(monkeypatch):
    import api_agent.workflows.definition as definitions
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)

    def forbidden(*args, **kwargs):
        pytest.fail('Admission must not load executable native bindings')

    monkeypatch.setattr(definitions, 'load_native_catalog', forbidden)

    admissions = capture_workflow_admissions(context)

    assert len(admissions) == 1
    identifier, selected = next(iter(admissions.items()))
    assert selected['definition_id'] == identifier
    assert selected['steps']
    assert selected['edges']


def test_explicit_admissions_are_detached_from_later_caller_mutation():
    expected = {'a' * 64: {'steps': [{'id': 'first'}]}}

    admitted = capture_workflow_admissions(None, expected)
    expected['a' * 64]['steps'][0]['id'] = 'replacement'

    assert admitted['a' * 64]['steps'] == [{'id': 'first'}]


def test_non_workflow_context_does_not_invent_an_admission(tmp_path):
    context = SimpleNamespace(base_root=tmp_path)

    assert capture_workflow_admissions(context) == {}


def test_missing_explicit_selection_fails_before_workers(tmp_path):
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        workflow_entry=tmp_path / 'missing.md.html', workflow_root='pack',
        workflow_replacements=())

    with pytest.raises(ResourcePackError, match='unavailable'):
        capture_workflow_admissions(context)


def test_recorded_runner_rejects_missing_selection_before_preparation(tmp_path):
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.recorded import run_recorded_integration
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
        workflow_entry=tmp_path / 'missing.md.html', workflow_root='pack',
        workflow_replacements=())
    test = TestIdentity('example::tests/integration/test_example.py::test_example',
        'example', 'tests/integration/test_example.py',
        'tests/integration/test_example.py::test_example')
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'artifacts'}

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid definition must fail before preparation or workers')

    with pytest.raises(ResourcePackError, match='unavailable'):
        run_recorded_integration(context, [test], config, accepted=True,
                                 prepare=forbidden, runner=forbidden)

    assert not (tmp_path / 'artifacts').exists()


@pytest.fixture
def profiled_workflow_admission():
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.test_profiles import resolve_test_profiles
    from api_agent.workflows.definition import load_definition
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root / 'demo/contrived/flow.md.html', base_root=root, pack_root=root / 'demo')
    nodes = ('tests/integration/test_workflow.py::test_workflow_node_coverage',
             'tests/integration/test_llm_request.py::test_llm_request')
    cases = [TestIdentity('api-agent::' + node, 'api-agent', node.split('::')[0], node, root=root)
             for node in nodes]
    application = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
                   'BEDROCK_REGION': 'us-east-1', 'WORKFLOW_MAX_PASSES': 2}
    profiles = resolve_test_profiles(cases, application, owner_root=root)
    plan = SimpleNamespace(definitions=(definition,),
        case_definitions={case.id: (definition.definition_id,) for case in cases})
    return definition, cases, application, profiles, plan


@pytest.mark.parametrize('reverse', [False, True])
def test_same_definition_admission_uses_each_trusted_case_profile(profiled_workflow_admission, reverse):
    from api_agent.testing.workflow_accounting import join_workflow_accounting
    from api_agent.workflows.execution import resolve_component_passes
    from api_agent.workflows.journal import WorkflowJournal
    definition, cases, application, profiles, plan = profiled_workflow_admission
    if reverse:
        profiles = dict(reversed(tuple(profiles.items())))
    admissions = capture_workflow_admissions(None, tool_plan=plan, profiles=profiles)
    assert set(admissions) == {(case.id, definition.definition_id) for case in cases}
    journals = {}
    for case, expected in zip(cases, (4, 2)):
        admitted = admissions[(case.id, definition.definition_id)]
        assert {row['max_passes'] for row in admitted['component_passes']} == {expected}
        passes, _ = resolve_component_passes(definition.layout, profiles[case.id]['config'])
        journals[case.id] = [WorkflowJournal(definition, component_passes=passes).finalize()]
    accounting = {'tests': [dict(test_id=case.id, calls=[], calls_complete=True) for case in cases]}
    joined, _ = join_workflow_accounting(journals, accounting, admissions=admissions)
    assert [row['routes'][0]['definition_binding'] for row in joined['tests']] == ['verified', 'verified']
    # A four-pass worker journal must not borrow another case's valid admission.
    mismatched, _ = join_workflow_accounting({cases[1].id: journals[cases[0].id]}, accounting,
                                            admissions=admissions)
    assert mismatched['tests'][0]['routes'][0]['definition_binding'] == 'unverified'
    assert application['WORKFLOW_MAX_PASSES'] == 2
    assert profiles[cases[0].id]['config']['WORKFLOW_MAX_PASSES'] == 4


def test_profile_admissions_fail_closed_for_unselected_or_empty_cases(profiled_workflow_admission):
    definition, cases, _, profiles, plan = profiled_workflow_admission
    plan.case_definitions = {cases[0].id: ()}
    assert capture_workflow_admissions(None, tool_plan=plan, profiles=profiles) == {}
    assert capture_workflow_admissions(None, tool_plan=plan, profiles={}) == {}


def test_profile_admission_for_selected_application_uses_effective_limits(profiled_workflow_admission, monkeypatch):
    import api_agent.runtime.resource_pack as packs
    definition, cases, _, profiles, _ = profiled_workflow_admission
    monkeypatch.setattr(packs, 'load_selected_definition', lambda context: definition)
    context = SimpleNamespace(workflow_entry=definition.entry)
    admissions = capture_workflow_admissions(context, profiles=profiles)
    assert {row['max_passes'] for row in admissions[(cases[0].id, definition.definition_id)]['component_passes']} == {4}
    assert {row['max_passes'] for row in admissions[(cases[1].id, definition.definition_id)]['component_passes']} == {2}


def test_explicit_admissions_remain_detached_with_profiles():
    supplied = {'a' * 64: {'component_passes': [{'id': 'first', 'max_passes': 2}]}}
    admitted = capture_workflow_admissions(None, supplied, profiles={'untrusted-unused': {}})
    supplied['a' * 64]['component_passes'][0]['max_passes'] = 4
    assert admitted['a' * 64]['component_passes'][0]['max_passes'] == 2


def test_recorded_runner_captures_admission_after_host_profile_resolution(profiled_workflow_admission, tmp_path, monkeypatch):
    from api_agent.testing.recorded import run_recorded_integration
    import api_agent.testing.workflow_admission as admission_owner
    import api_agent.providers.rates as rates
    import api_agent.testing.cost_context as costs
    definition, cases, application, _, plan = profiled_workflow_admission
    captured = []
    original = admission_owner.capture_workflow_admissions
    class AdmissionCaptured(Exception):
        pass
    def capture(context, supplied=None, *, tool_plan=None, profiles=None):
        captured.append(original(context, supplied, tool_plan=tool_plan, profiles=profiles))
        raise AdmissionCaptured
    def forbidden(*args, **kwargs):
        pytest.fail('Admission must precede provider preparation and workers')
    monkeypatch.setattr(admission_owner, 'capture_workflow_admissions', capture)
    monkeypatch.setattr(rates, 'freeze_pricing', lambda *args: {})
    monkeypatch.setattr(costs, 'cost_config', lambda context, values: dict(values))
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    config = dict(application, TUI_MODE='integration', ALLURE_S3_BUCKET='synthetic',
                  ALLURE_RESULTS_DIR=tmp_path / 'artifacts')
    with pytest.raises(AdmissionCaptured):
        run_recorded_integration(context, cases, config, accepted=True, tool_plan=plan,
                                 prepare=forbidden, runner=forbidden)
    assert len(captured) == 1
    assert {row['max_passes'] for row in captured[0][(cases[0].id, definition.definition_id)]['component_passes']} == {4}
    assert {row['max_passes'] for row in captured[0][(cases[1].id, definition.definition_id)]['component_passes']} == {2}
