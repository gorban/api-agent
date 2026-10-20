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
