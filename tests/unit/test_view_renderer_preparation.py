"""Live workflow views prepare their renderer instead of requiring a manual step."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.runtime import mise_tools, view_flow
from api_agent.workflows import viewer_build, viewer_config, viewer_render


@pytest.fixture
def live_view(tmp_path, monkeypatch):
    """A selection that differs from the saved diagrams, on a host without a cached renderer."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('API_AGENT_PLANTUML_JAR', raising=False)
    definition = SimpleNamespace(roots={'base': tmp_path})
    canonical = {'value': False}
    monkeypatch.setattr(view_flow, 'load_selected_definition', lambda context: definition)
    monkeypatch.setattr(view_flow, '_canonical_selection', lambda *args: canonical['value'])
    monkeypatch.setattr('api_agent.workflows.viewer_projection.project_view', lambda definition: object())
    monkeypatch.setattr(viewer_render, 'capture_repository_sources', lambda roots: ({}, ()))
    monkeypatch.setattr(viewer_config, 'resolve_renderer_path', lambda context=None: '')
    calls = []
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *names, **kwargs: calls.append(('tools', names)))
    jar = tmp_path / 'prepared.jar'
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: calls.append(('renderer', None)) or jar)
    monkeypatch.setattr(viewer_render, 'PlantUMLRenderer', lambda path: ('renderer', path))
    bundles = []
    monkeypatch.setattr(viewer_render, 'build_view_bundle', lambda *args, **kwargs: bundles.append(kwargs) or object())
    return SimpleNamespace(calls=calls, jar=jar, bundles=bundles, canonical=canonical)


def test_a_live_view_prepares_the_pinned_renderer_and_project_java(live_view):
    view_flow._prepare_view(object())

    assert live_view.calls == [('tools', ('java',)), ('renderer', None)]
    assert live_view.bundles[0]['renderer'] == ('renderer', live_view.jar)


def test_a_configured_renderer_is_used_without_preparing_another(live_view, monkeypatch):
    monkeypatch.setattr(viewer_render, 'default_renderer', lambda path: ('configured', path))

    view_flow._prepare_view(object(), renderer_path='/configured/renderer.jar')

    assert live_view.calls == [('tools', ('java',))]
    assert live_view.bundles[0]['renderer'] == ('configured', '/configured/renderer.jar')


def test_a_configured_renderer_that_is_missing_is_not_replaced(live_view, tmp_path):
    with pytest.raises(viewer_render.RenderError, match='PlantUML renderer missing'):
        view_flow._prepare_view(object(), renderer_path=str(tmp_path / 'missing.jar'))

    assert live_view.calls == [('tools', ('java',))]


def test_view_tools_are_prepared_only_for_a_live_view(live_view):
    view_flow.prepare_view_tools(object())
    assert live_view.calls == [('tools', ('java',)), ('renderer', None)]

    live_view.calls.clear()
    live_view.canonical['value'] = True
    view_flow.prepare_view_tools(object())
    assert live_view.calls == []


def _local_owner(tmp_path, **options):
    from api_agent.runtime.local_run import LocalRunOwner
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    config = SimpleNamespace(errors=(), missing=(), values={'UI_PROVIDER': 'selected'})
    return LocalRunOwner(context, lambda: config, **options), context


def test_local_run_prepares_view_tools_outside_its_short_budget(tmp_path, monkeypatch):
    owner, context = _local_owner(tmp_path)
    async def no_workspaces(*args):
        return ()
    monkeypatch.setattr('api_agent.runtime.workspace_preparation.aprepare_selected_workspaces', no_workspaces)
    seen = []
    async def frontend():
        seen.append('frontend')
    monkeypatch.setattr('api_agent.runtime.frontend.prepare_source_frontend', frontend)
    def prepare(context_):
        seen.append(context_)
        raise OSError('renderer prerequisite unavailable')
    monkeypatch.setattr(view_flow, 'prepare_view_tools', prepare)
    monkeypatch.setattr('api_agent.runtime.local_run.component_timeout',
                        lambda *args: pytest.fail('renderer preparation must not use the 120 second budget'))
    with pytest.raises(OSError, match='renderer prerequisite unavailable'):
        asyncio.run(owner.start())
    assert seen == ['frontend', context]


def test_local_run_with_its_own_view_preparation_skips_view_tools(tmp_path, monkeypatch):
    owner, _ = _local_owner(tmp_path, prepare_view=lambda context: pytest.fail('stopped before the view'))
    async def no_workspaces(*args):
        return ()
    monkeypatch.setattr('api_agent.runtime.workspace_preparation.aprepare_selected_workspaces', no_workspaces)
    monkeypatch.setattr(view_flow, 'prepare_view_tools', lambda context: pytest.fail('custom view preparation'))
    class Stop(Exception):
        pass
    def budget(*args):
        raise Stop()
    monkeypatch.setattr('api_agent.runtime.local_run.component_timeout', budget)
    with pytest.raises(Stop):
        asyncio.run(owner.start())
