"""The container runtime exposes the reaper for a one-time manual cleanup."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.reaper import DEFAULT_RETENTION_DAYS, ReapReport
from test_managed_reaper import CURRENT, OLD, Docker, checkout
from test_tui import fixture


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('api_agent.runtime.reaper.host_paths_visible', lambda environment: True)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: frozenset())
    docker = Docker()
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path / 'mine')
    run = docker.run

    def routed(command, deadline=None):
        if command[1] == 'network':
            docker.commands.append(command)
            return ''
        return run(command, deadline)

    docker.run = routed
    monkeypatch.setattr(runtime, '_run', lambda arguments, **options: docker.run(('docker', *arguments), options['deadline']))
    return docker, runtime


def test_manual_cleanup_removes_an_orphaned_scope_through_the_runtime(engine, tmp_path):
    docker, runtime = engine
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.volume('sa-aaaaaaaaaaaaaaaa_data', OLD)
    kept = docker.container(CURRENT, working_dir=checkout(tmp_path, CURRENT, exists=False))

    report = runtime.reap_unused()

    assert (report.scopes, report.containers, report.volumes, report.diagnostic) == (1, 1, 1, '')
    assert list(docker.containers) == [kept] and not docker.volumes
    assert ('docker', 'network', 'ls', '--filter', 'label=api-agent.network-cleanup-v1=' + OLD,
            '--format', '{{.Name}}') in docker.commands


@pytest.mark.parametrize(('retention', 'reaped'), [(DEFAULT_RETENTION_DAYS, True), (0, False)])
def test_manual_cleanup_honours_the_retention_period(engine, tmp_path, retention, reaped):
    docker, runtime = engine
    docker.container(OLD, working_dir=checkout(tmp_path, OLD), finished_days=40000)

    runtime.reap_unused(retention_days=retention)

    assert (not docker.containers) is reaped


def test_manual_cleanup_reports_engine_failure_without_raising(engine, tmp_path):
    docker, runtime = engine
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.fail = ('ps', '-a')

    assert runtime.reap_unused().diagnostic == 'incomplete: PrerequisiteError'


@pytest.mark.parametrize('runtime', [ContainerRuntime('docker'),
                                     ContainerRuntime('docker', StackScope(CURRENT))])
def test_manual_cleanup_requires_scope_and_ownership_directory(runtime):
    with pytest.raises(ContainerError, match='scope and ownership directory'):
        runtime.reap_unused()


@pytest.mark.parametrize('seconds', [0, -1, 301, float('nan'), '5'])
def test_manual_cleanup_deadline_is_bounded(engine, seconds):
    with pytest.raises(ContainerError, match='between zero and 300 seconds'):
        engine[1].reap_unused(cleanup_timeout_seconds=seconds)


def reaping_app(tmp_path, runtime, **fields):
    app = fixture(tmp_path, 'cleanup-containers', fields=fields)
    app.container_runtime = runtime
    return app


def test_container_management_removes_unused_resources_and_reports_counts(tmp_path):
    calls = []
    def reap_unused(**options):
        calls.append(options)
        return ReapReport(2, 5, 3, 2)
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=reap_unused)
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('reap-containers')
            await pilot.pause()
            await pilot.pause()
            assert 'Removed 2 unused scope(s): 5 container(s), 3 volume(s), 2 network(s).' in str(
                app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())
    assert calls == [{'retention_days': DEFAULT_RETENTION_DAYS}]


def test_container_management_passes_the_configured_retention(tmp_path):
    calls = []
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: calls.append(options) or ReapReport())
    async def scenario():
        app = reaping_app(tmp_path, runtime, MANAGED_CONTAINER_RETENTION_DAYS={'type': 'int', 'default': 3, 'acknowledge_default': False})
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('reap-containers')
            await pilot.pause()
            await pilot.pause()
            assert 'Nothing unused' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())
    assert calls == [{'retention_days': 3}]


@pytest.mark.parametrize(('outcome', 'expected'), [
    (ReapReport(1, 1, 0, 0, 'incomplete: deadline'), 'incomplete: deadline'),
    (ContainerError('Container engine unavailable.'), 'Container engine unavailable.')])
def test_container_management_shows_incomplete_or_failed_cleanup(tmp_path, outcome, expected):
    def reap_unused(**options):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=reap_unused)
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('reap-containers')
            await pilot.pause()
            await pilot.pause()
            assert expected in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())
