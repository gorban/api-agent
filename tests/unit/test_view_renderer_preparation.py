"""Live workflow views prepare their renderer through mise instead of a manual step."""
import asyncio
from hashlib import sha256
import io
import json
import multiprocessing.process
import os
import sys
from types import SimpleNamespace

import pytest

from api_agent.runtime import mise_tools, view_flow
from api_agent.workflows import viewer_build, viewer_config, viewer_render


@pytest.fixture
def mise_renderer(fake_mise, tmp_path, monkeypatch):
    """The fake mise's installed PlantUML release file, pinned as the expected JAR."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    jar = tmp_path / 'installs/plantuml/plantuml-mit-1.2026.9.jar'
    monkeypatch.setattr(viewer_build, 'PLANTUML_SHA256', sha256(jar.read_bytes()).hexdigest())
    return SimpleNamespace(jar=jar, tools=fake_mise[2], log=fake_mise[3])


def test_renderer_preparation_starts_no_worker_while_stderr_has_no_descriptor(mise_renderer, monkeypatch):
    class CapturedStderr(io.StringIO):
        def fileno(self):
            return -1
    monkeypatch.setattr(sys, 'stderr', CapturedStderr())
    monkeypatch.setattr(multiprocessing.process.BaseProcess, 'start',
                        lambda self: pytest.fail('renderer preparation started a worker process'))

    assert viewer_build.prepare_renderer() == mise_renderer.jar


def test_renderer_preparation_installs_java_and_the_pinned_jar_through_mise(mise_renderer):
    assert viewer_build.prepare_renderer() == mise_renderer.jar

    calls = [json.loads(line)[0] for line in mise_renderer.log.read_text().splitlines()]
    assert calls[0][-3:] == ['install', 'core:java@temurin-21.0.13+9.0.LTS', 'github:plantuml/plantuml@1.2026.9']
    assert calls[1][-4:] == ['which', '--tool', 'core:java@temurin-21.0.13+9.0.LTS', 'java']
    assert calls[2][-2:] == ['where', 'github:plantuml/plantuml@1.2026.9']
    assert len(calls) == 3
    assert os.environ['PATH'].split(os.pathsep)[0] == str(mise_renderer.tools)
    assert os.environ['JAVA_HOME'] == str(mise_renderer.tools.parent)


def test_renderer_preparation_rejects_a_jar_that_differs_from_the_pin(mise_renderer, monkeypatch):
    monkeypatch.setattr(viewer_build, 'PLANTUML_SHA256', '0' * 64)
    with pytest.raises(viewer_render.RenderError, match='PlantUML artifact digest mismatch'):
        viewer_build.prepare_renderer()


def test_renderer_preparation_outside_the_launcher_names_the_supported_paths(mise_renderer, monkeypatch):
    monkeypatch.delenv('API_AGENT_MISE')
    with pytest.raises(viewer_render.RenderError, match=r'\./launch\.sh.*API_AGENT_PLANTUML_JAR'):
        viewer_build.prepare_renderer()
    assert not mise_renderer.log.exists()


def test_renderer_preparation_installs_within_its_reported_budget(tmp_path, monkeypatch):
    from api_agent.providers import progress
    jar = tmp_path / 'renderer.jar'
    jar.write_bytes(b'renderer')
    monkeypatch.setattr(viewer_build, 'PLANTUML_SHA256', sha256(b'renderer').hexdigest())
    seen, reports = [], []
    def ensure(*names):
        seen.append((names, progress._scope.get()))
        return {'plantuml': jar}
    monkeypatch.setattr(mise_tools, 'ensure_tools', ensure)

    with progress.progress_sink(reports.append):
        assert viewer_build.prepare_renderer() == jar
    names, budget = seen[0]
    assert names == ('java', 'plantuml')
    assert reports[0].name == 'Workflow renderer preparation'
    assert budget.id == reports[0].id and 0 < budget.timeout <= 300


def test_an_explicit_renderer_prepares_only_java(tmp_path, monkeypatch):
    jar = tmp_path / 'explicit.jar'
    jar.write_bytes(b'explicit')
    calls = []
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *names, **kwargs: calls.append(names))
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: pytest.fail('explicit renderer ignored'))

    assert viewer_render.default_renderer(str(jar)).jar == jar
    assert calls == [('java',)]


def test_a_missing_explicit_renderer_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *names, **kwargs: pytest.fail('unexpected mise'))
    with pytest.raises(viewer_render.RenderError, match='PlantUML renderer missing.*API_AGENT_PLANTUML_JAR'):
        viewer_render.default_renderer(str(tmp_path / 'missing.jar'))


def test_an_unconfigured_renderer_uses_the_mise_prepared_jar(tmp_path, monkeypatch):
    jar = tmp_path / 'prepared.jar'
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: jar)
    assert viewer_render.default_renderer('').jar == jar


@pytest.fixture
def live_view(tmp_path, monkeypatch):
    """A selection that differs from the saved diagrams."""
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


def test_a_live_view_prepares_the_pinned_renderer(live_view):
    view_flow._prepare_view(object())

    assert live_view.calls == [('renderer', None)]
    assert live_view.bundles[0]['renderer'] == ('renderer', live_view.jar)


def test_a_configured_renderer_is_used_without_preparing_another(live_view, tmp_path):
    jar = tmp_path / 'configured.jar'
    jar.write_bytes(b'configured')

    view_flow._prepare_view(object(), renderer_path=str(jar))

    assert live_view.calls == [('tools', ('java',))]
    assert live_view.bundles[0]['renderer'] == ('renderer', jar)


def test_a_configured_renderer_that_is_missing_is_not_replaced(live_view, tmp_path):
    with pytest.raises(viewer_render.RenderError, match='PlantUML renderer missing'):
        view_flow._prepare_view(object(), renderer_path=str(tmp_path / 'missing.jar'))

    assert live_view.calls == []


def test_view_tools_are_prepared_only_for_a_live_view(live_view):
    view_flow.prepare_view_tools(object())
    assert live_view.calls == [('renderer', None)]

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
