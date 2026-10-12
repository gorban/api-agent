"""Accepted execution prepares only selected provider distributions."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.testing import launched
from api_agent.testing.collection import TestIdentity
from api_agent.testing.prerequisites import local_recording_prerequisites


def provider(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    factory = root / 'factory.py'
    factory.write_text('raise AssertionError("Declaration inspection must not import")\n')
    (root / 'provider-dependencies.toml').write_text(
        f'version=1\ndependencies=["{name}-transport>=1"]\n')
    return str(factory) + ':factory'


@pytest.mark.parametrize('pack_api', [False, True])
def test_accepted_integration_passes_selection_capabilities(tmp_path, monkeypatch, pack_api):
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, pack_root=tmp_path)
    tags = ('requires_pack_api',) if pack_api else ()
    test = TestIdentity('base::tests/integration/test_case.py::test_case', 'base',
                        'tests/integration/test_case.py', 'tests/integration/test_case.py::test_case', tags)
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic'}
    hooks = Mock(pending_cleanup=None)
    build = Mock(return_value=hooks)
    monkeypatch.setattr(launched, 'local_recording_prerequisites', build)
    monkeypatch.setattr('api_agent.testing.recorded.run_recorded_integration', Mock(return_value='result'))

    assert launched.run_launched_integration(context, [test], config, accepted=True) == 'result'

    expected = {'llm', 'api', 'upstream', 'access'} if pack_api else {'llm'}
    assert build.call_args.kwargs['provider_kinds'] == expected
    assert build.call_args.kwargs['provider_config'] is config
    assert 'ui' not in build.call_args.kwargs['provider_kinds']
    hooks.finish.assert_called_once()


def test_recording_preparation_is_lazy_and_skips_inactive_selections(tmp_path, monkeypatch):
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, pack_root=tmp_path,
                              external_pack=False)
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'LLM_PROVIDER': provider(tmp_path, 'example'),
              'API_PROVIDER': 'missing.py:factory', 'UI_PROVIDER': 'missing.py:factory'}
    ensure = Mock(return_value=None)
    monkeypatch.setattr('api_agent.runtime.dependencies.active_environment', lambda path: True)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', ensure)
    monkeypatch.setattr('api_agent.runtime.compose.plan_compose', Mock(return_value='plan'))
    manager = Mock()
    manager.acquire.return_value = 'lease'
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', Mock(return_value=manager))
    owner = local_recording_prerequisites(context, scope='scope', ownership_directory=tmp_path,
        provider_config=config, provider_kinds={'llm'})
    ensure.assert_not_called()

    owner.prepare(('selected',), config, accepted=True)

    manifests = ensure.call_args.kwargs['selected_provider_manifests']
    assert len(manifests) == 1
    assert manifests[0].dependencies == ('example-transport>=1',)
    assert manifests[0].root == Path(config['LLM_PROVIDER'].split(':')[0]).parent
    owner.finish()


@pytest.mark.parametrize('values,expected', [
    ({'TUI_MODE': 'unit'}, set()),
    ({'TUI_MODE': 'integration', 'COLLECT_ONLY': True}, set()),
    ({'TUI_MODE': 'run-local', 'HELP': True}, set()),
    ({'TUI_MODE': 'run-local', 'RUN_UI': False}, {'api', 'llm', 'access', 'upstream'}),
    ({'TUI_MODE': 'run-local', 'RUN_UI': True}, {'api', 'llm', 'access', 'upstream', 'ui'}),
])
def test_local_operation_environment_uses_activation_contract(tmp_path, monkeypatch, values, expected):
    from api_agent.runtime.operation import local_operation
    from api_agent.providers.distribution import PROVIDER_SELECTORS
    for kind, field in PROVIDER_SELECTORS.items():
        values[field] = provider(tmp_path, kind) if kind in expected else 'unavailable.py:factory'
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, external_pack=False)
    result = SimpleNamespace(values=values, errors=(), missing=())
    ensure = Mock(return_value=None)
    monkeypatch.setattr('api_agent.runtime.dependencies.active_environment', lambda path: True)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', ensure)
    owner = local_operation(context, resolve=lambda: result, argv=(), scope='scope',
                            ownership_directory=tmp_path)
    ensure.assert_not_called()

    owner.prepare_dependencies(result)

    manifests = ensure.call_args.kwargs.get('selected_provider_manifests', ())
    assert {manifest.root.name for manifest in manifests} == expected
