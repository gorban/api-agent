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
            assert 'Stop selected containers (s)' in str(app.screen.query_one('#stop-containers').label)
            assert app.screen.check_action('stop_selected', ())
            await pilot.press('s')
            await pilot.pause()
            assert stopped == []
            app.screen.query_one(Input).value = 'yes'
            await pilot.press('enter')
            await pilot.pause()
            assert stopped == []
            await pilot.press('s')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert stopped == [('abc',)]
            assert app.screen.query_one(DataTable).row_count == 2
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


def test_container_all_selection_tracks_individuals_and_empty_inventory(tmp_path):
    async def scenario(count):
        app = fixture(tmp_path / str(count), 'cleanup-containers')
        items = tuple(Container(str(i), 'service', 'image', 'Up') for i in range(count))
        app.container_runtime = SimpleNamespace(inventory=lambda: items)
        async with app.run_test() as pilot:
            await pilot.pause()
            if not count:
                assert not app.screen.query(DataTable)
                return
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0)[1] == 'All'
            assert table.get_row_at(0)[0] == ''
            await pilot.press('enter')
            assert app.container_marked == {item.id for item in items}
            assert all(table.get_row_at(i)[0] == '✓' for i in range(table.row_count))
            await pilot.press('enter')
            assert not app.container_marked
            for item in items:
                app.toggle_row(item.id)
            assert table.get_row_at(0)[0] == '✓'
            app.toggle_row(items[0].id)
            assert table.get_row_at(0)[0] == ''
            assert 'container-all' not in app.container_marked
    for count in (0, 1, 2):
        asyncio.run(scenario(count))


def test_inventory_excludes_hosting_container_by_name_and_abbreviated_id():
    identity = 'f0742ea87cfe6540535e5790f2e58171409cbbdfba742bf660ea3a1e2fd35892'
    output = '\n'.join((
        '{"ID":"f0742ea87cfe","Names":"workbench-host"}',
        '{"ID":"different","Names":["workbench-host"]}',
        '{"ID":"abc","Names":"api"}'))
    runtime = ContainerRuntime('/bin/docker')
    with patch('api_agent.runtime.containers.session_container_identity', return_value={identity, 'workbench-host'}), patch.object(runtime, '_run', return_value=output):
        assert [item.name for item in runtime.inventory()] == ['api']


def test_stop_rejects_hosting_container_even_without_inventory():
    identity = 'f0742ea87cfe6540535e5790f2e58171409cbbdfba742bf660ea3a1e2fd35892'
    runtime = ContainerRuntime('/bin/docker')
    with patch('api_agent.runtime.containers.session_container_identity', return_value={identity, 'workbench-host'}), patch.object(runtime, '_run') as run:
        for identifier in (identity, identity[:12], 'workbench-host', '/workbench-host'):
            with pytest.raises(ContainerError, match='hosting this session'):
                runtime.stop(['abc', identifier])
        run.assert_not_called()
        runtime.stop(['abc'])
        run.assert_called_once_with(['stop', 'abc'])


def test_session_container_metadata_is_parsed_without_shell_execution():
    from api_agent.runtime.containers import session_container_identity
    with patch.dict('os.environ', {'CONTAINER_ID': 'workbench-host', 'CONTAINER_NAME': ''}), patch('api_agent.runtime.containers.Path.read_text', return_value='id="abcdef123456"\nname="workbench-host"\nimage="ignored"\n'):
        assert session_container_identity() == {'workbench-host', 'abcdef123456'}


def test_menu_container_button_empty_running_and_unavailable(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        items = []
        def inventory():
            if items is None:
                raise ContainerError('Container inventory unavailable: fixture failure.')
            return tuple(items)
        app.container_runtime = SimpleNamespace(inventory=inventory)
        async with app.run_test() as pilot:
            await pilot.pause()
            button = app.screen.query_one('#mode-cleanup-containers')
            assert str(button.label) == 'No running containers to stop'
            assert button.disabled
            items.append(Container('abc', 'api', 'image', 'Up'))
            app.refresh_container_inventory()
            await pilot.pause()
            assert str(button.label) == 'Stop running containers'
            assert not button.disabled
            assert 'Running containers: 1' in str(app.screen.query_one('#body', Static).content)
            items = None
            app.refresh_container_inventory()
            await pilot.pause()
            assert str(button.label) == 'Stop running containers'
            assert not button.disabled
            assert 'unavailable' in str(app.screen.query_one('#body', Static).content)
    asyncio.run(scenario())
