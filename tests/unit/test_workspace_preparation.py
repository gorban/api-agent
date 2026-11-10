"""Selected providers' declared workspaces are prepared from inert manifests only."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.runtime import workspace_preparation as preparation


@pytest.fixture
def provider(tmp_path):
    root = tmp_path / 'provider'
    root.mkdir()
    (root / 'factory.py').write_text('raise RuntimeError("factory must stay unimported")\n')
    (root / 'provider-dependencies.toml').write_text(
        'version = 1\n[[node_workspaces]]\npath_field = "WORKSPACE_DIR"\ncredential_fields = ["REGISTRY_TOKEN"]\n'
        'ready = ["node_modules/.bin/tool"]\nenvironment = { SKIP_HOOKS = "1" }\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    values = {'UI_PROVIDER': f'{root / "factory.py"}:create', 'WORKSPACE_DIR': str(tmp_path / 'workspace'),
              'REGISTRY_TOKEN': 'secret-value'}
    return context, values


def test_declared_workspace_is_prepared_with_named_credentials_only(provider, monkeypatch, tmp_path):
    context, values = provider
    calls = []
    monkeypatch.setattr('api_agent.runtime.node_workspace.ensure_node_workspace',
                        lambda root, **options: calls.append((root, options)))
    prepared = preparation.prepare_selected_workspaces(context, values, frozenset({'ui'}), cancellation='stop')
    assert prepared == ((tmp_path / 'workspace').resolve(),)
    root, options = calls[0]
    assert options['ready'] == ('node_modules/.bin/tool',) and options['cancellation'] == 'stop'
    assert options['credentials'] == {'REGISTRY_TOKEN': 'secret-value'}
    assert dict(options['environment']) == {'SKIP_HOOKS': '1'}


def test_wrapped_and_blank_values_are_read_by_declared_name(provider, monkeypatch):
    context, values = provider
    values['WORKSPACE_DIR'] = SimpleNamespace(value=values['WORKSPACE_DIR'])
    values['REGISTRY_TOKEN'] = '  '
    calls = []
    monkeypatch.setattr('api_agent.runtime.node_workspace.ensure_node_workspace',
                        lambda root, **options: calls.append(options))
    preparation.prepare_selected_workspaces(context, values, frozenset({'ui'}))
    assert calls[0]['credentials'] == {'REGISTRY_TOKEN': None}


def test_nothing_is_prepared_without_kinds_or_declarations(provider, monkeypatch):
    context, values = provider
    monkeypatch.setattr('api_agent.runtime.node_workspace.ensure_node_workspace',
                        lambda *a, **k: pytest.fail('must not prepare'))
    assert preparation.prepare_selected_workspaces(context, values, frozenset()) == ()
    assert preparation.prepare_selected_workspaces(context, values, frozenset({'llm'})) == ()
    assert preparation.prepare_selected_workspaces(context, {}, frozenset({'ui'})) == ()


def test_missing_workspace_selection_is_a_named_error(provider):
    context, values = provider
    del values['WORKSPACE_DIR']
    with pytest.raises(ValueError, match='WORKSPACE_DIR must select'):
        preparation.prepare_selected_workspaces(context, values, frozenset({'ui'}))


def test_two_providers_naming_one_workspace_install_it_once(provider, monkeypatch, tmp_path):
    context, values = provider
    second = tmp_path / 'second'
    second.mkdir()
    (second / 'factory.py').write_text('')
    (second / 'provider-dependencies.toml').write_text((tmp_path / 'provider/provider-dependencies.toml').read_text())
    values['LLM_PROVIDER'] = f'{second / "factory.py"}:create'
    calls = []
    monkeypatch.setattr('api_agent.runtime.node_workspace.ensure_node_workspace',
                        lambda root, **options: calls.append(root))
    assert len(preparation.prepare_selected_workspaces(context, values, frozenset({'ui', 'llm'}))) == 1
    assert len(calls) == 1


def test_async_preparation_runs_off_the_event_loop_with_cancellation(provider, monkeypatch):
    context, values = provider
    seen = {}
    def prepare(context_, values_, kinds, *, cancellation):
        seen.update(args=(context_, values_, kinds), cancellation=cancellation)
    monkeypatch.setattr(preparation, 'prepare_selected_workspaces', prepare)
    asyncio.run(preparation.aprepare_selected_workspaces(context, values, frozenset({'ui'})))
    assert seen['args'] == (context, values, frozenset({'ui'})) and not seen['cancellation'].is_set()


def _local_owner(tmp_path, *, valid):
    from api_agent.runtime.local_run import LocalRunOwner
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    config = SimpleNamespace(errors=() if valid else ('bad',), missing=(), values={'UI_PROVIDER': 'selected'})
    return LocalRunOwner(context, lambda: config), context, config


def test_local_run_prepares_declared_workspaces_outside_its_short_budget(tmp_path, monkeypatch):
    owner, context, config = _local_owner(tmp_path, valid=True)
    seen = []
    async def prepare(context_, values, kinds):
        seen.append((context_, values, kinds))
        raise OSError('workspace prerequisite unavailable')
    monkeypatch.setattr(preparation, 'aprepare_selected_workspaces', prepare)
    monkeypatch.setattr('api_agent.runtime.local_run.component_timeout',
                        lambda *args: pytest.fail('workspace install must not use the 120 second budget'))
    with pytest.raises(OSError, match='workspace prerequisite unavailable'):
        asyncio.run(owner.start())
    assert seen == [(context, config.values, frozenset({'ui'}))]


def test_local_run_skips_workspace_preparation_for_invalid_configuration(tmp_path, monkeypatch):
    owner, context, config = _local_owner(tmp_path, valid=False)
    async def prepare(*args):
        pytest.fail('invalid configuration must not install anything')
    monkeypatch.setattr(preparation, 'aprepare_selected_workspaces', prepare)
    with pytest.raises(ValueError, match='invalid_runtime_configuration'):
        asyncio.run(owner.start())


def test_resolved_path_values_select_the_workspace(provider, monkeypatch, tmp_path):
    from api_agent.config import load_shared_schema
    from api_agent.config.resolve import convert_value
    context, values = provider
    values['WORKSPACE_DIR'] = convert_value(load_shared_schema().fields['UI_SOURCE_DIR'], 'workspace',
                                            external=False, anchors={'owner_root': tmp_path})
    calls = []
    monkeypatch.setattr('api_agent.runtime.node_workspace.ensure_node_workspace',
                        lambda root, **options: calls.append(root))
    assert preparation.prepare_selected_workspaces(context, values, frozenset({'ui'})) == (
        (tmp_path / 'workspace').resolve(),)
    assert calls == [(tmp_path / 'workspace').resolve()]
