import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from api_agent.runtime.containers import Container, ContainerError, ContainerRuntime
from textual.widgets import DataTable, Input, Static
from test_tui import fixture


def test_inventory_uses_docker_compatible_format_and_bounded_timeout():
    result = SimpleNamespace(returncode=0, stdout='{"ID":"abc","Names":"api","Image":"api:dev","Status":"Up"}\n')
    with patch('api_agent.runtime.containers.shutil.which', return_value='/bin/docker'), patch('api_agent.runtime.containers.subprocess.run', return_value=result) as run:
        assert ContainerRuntime().inventory() == (Container('abc', 'api', 'api:dev', 'Up'),)
        run.assert_called_once_with(['/bin/docker', 'ps', '--format', '{{json .}}'], capture_output=True, text=True, timeout=5, check=False)


def test_inventory_falls_back_only_when_docker_absent_and_failure_is_not_zero():
    with patch('api_agent.runtime.containers.shutil.which', side_effect=[None, '/bin/podman']), patch('api_agent.runtime.containers.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='')) as run:
        with pytest.raises(ContainerError, match='exit 1'):
            ContainerRuntime().inventory()
        assert run.call_args.args[0][0] == '/bin/podman'
    with patch('api_agent.runtime.containers.shutil.which', return_value=None):
        with pytest.raises(ContainerError, match='absent'):
            ContainerRuntime().inventory()


def test_stop_only_passes_selected_ids():
    with patch('api_agent.runtime.containers.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='')) as run:
        ContainerRuntime('/bin/docker').stop(['abc', 'def'])
        assert run.call_args.args[0] == ['/bin/docker', 'stop', 'abc', 'def']


def test_cleanup_requires_literal_y_and_refreshes(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'cleanup-containers')
        stopped = []
        items = [Container('abc', 'api', 'image', 'Up'), Container('def', 'db', 'image', 'Up')]
        def stop(ids):
            stopped.append(ids)
            items[:] = [item for item in items if item.id not in ids]
        app.container_runtime = SimpleNamespace(inventory=lambda: tuple(items), stop=stop)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'containers'
            app.toggle_row('abc')
            await app.choose('stop-containers')
            await pilot.pause()
            assert stopped == []
            app.screen.query_one(Input).value = 'yes'
            await pilot.press('enter')
            await pilot.pause()
            assert stopped == []
            await app.choose('stop-containers')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert stopped == [('abc',)]
            assert app.screen.query_one(DataTable).row_count == 1
            await app.choose('menu')
            await pilot.pause()
            assert 'Running containers: 1' in str(app.screen.query_one('#body', Static).content)
    asyncio.run(scenario())


def test_inventory_error_is_visible(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'cleanup-containers')
        def fail():
            raise ContainerError('Container inventory unavailable: fixture failure.')
        app.container_runtime = SimpleNamespace(inventory=fail)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert 'unavailable' in str(app.screen.query_one('#body', Static).content)
            assert not app.containers
    asyncio.run(scenario())


def test_timeout_and_invalid_inventory_are_unavailable():
    import subprocess
    with patch('api_agent.runtime.containers.subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 5)):
        with pytest.raises(ContainerError, match='TimeoutExpired'):
            ContainerRuntime('/bin/docker').inventory()
    with patch('api_agent.runtime.containers.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='not json')):
        with pytest.raises(ContainerError, match='invalid engine response'):
            ContainerRuntime('/bin/docker').inventory()
