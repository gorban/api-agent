import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from api_agent.runtime.containers import Container, ContainerError, ContainerRuntime
from textual.widgets import DataTable, Input, Static
from test_tui import fixture








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

from api_agent.runtime.ownership import StackScope
import json
import subprocess

SCOPE = StackScope('0123456789abcdef')


def record(identifier='a' * 64, **changes):
    value = {'Id': identifier, 'Name': '/api', 'Config': {'Image': 'api:dev', 'Labels': {**SCOPE.labels('otel'), 'com.docker.compose.project': SCOPE.project, 'com.docker.compose.service': 'api'}}, 'State': {'Running': True, 'Status': 'running'}}
    value.update(changes)
    return value


def test_unscoped_runtime_never_calls_engine():
    with patch('api_agent.runtime.containers.subprocess.run') as run:
        assert ContainerRuntime().inventory() == ()
        with pytest.raises(ContainerError, match='scope'):
            ContainerRuntime().stop(['a'])
        run.assert_not_called()


def test_scoped_inventory_and_fresh_stop_use_inspect_and_full_ids():
    item = record()
    with patch('api_agent.runtime.containers.session_container_identity', return_value=set()), patch('api_agent.runtime.containers.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout=item['Id']), SimpleNamespace(returncode=0, stdout=json.dumps([item])), SimpleNamespace(returncode=0, stdout=json.dumps([item])), SimpleNamespace(returncode=0, stdout='')]) as run:
        runtime = ContainerRuntime('/bin/docker', SCOPE)
        assert runtime.inventory() == (Container(item['Id'], 'api', 'api:dev', 'running'),)
        runtime.stop([item['Id'], item['Id']])
        assert [call.args[0][1:] for call in run.call_args_list] == [['ps', '--quiet', '--no-trunc', '--filter', 'label=api-agent.managed=true'], ['inspect', item['Id']], ['inspect', item['Id']], ['stop', '--time', '10', item['Id']]]
        assert all(call.kwargs == dict(capture_output=True,text=True,timeout=5,check=False) for call in run.call_args_list[:-1])
        assert run.call_args_list[-1].kwargs == dict(capture_output=True,text=True,timeout=17,check=False)


def test_inventory_filters_unowned_stopped_session_and_distrobox():
    items = [record(), record('b'*64, State={'Running':False}), record('c'*64, Name='/session'), record('d'*64, Config={'Labels':{}}), record('e'*64)]
    items[-1]['Config']['Labels']['distrobox.name'] = 'dev'
    runtime = ContainerRuntime('docker', SCOPE)
    with patch('api_agent.runtime.containers.session_container_identity', return_value={'session'}), patch.object(runtime, '_run', side_effect=['ids',json.dumps(items)]):
        assert [item.id for item in runtime.inventory()] == ['a'*64]
    with patch.object(runtime, '_run', return_value=''):
        assert runtime.inventory() == ()


@pytest.mark.parametrize('items', [[], [record('b'*64)], [record(Config={'Labels':{}})], [record(Name='/session')]])
def test_stop_revalidates_exact_owned_identity(items):
    runtime = ContainerRuntime('docker', SCOPE)
    with patch('api_agent.runtime.containers.session_container_identity', return_value={'session'}), patch.object(runtime,'_run',return_value=json.dumps(items)) as run:
        with pytest.raises(ContainerError,match='no longer owned'):
            runtime.stop(['a'*64])
        assert run.call_count == 1


@pytest.mark.parametrize('ids', [[], [''], ['-a'], [None]])
def test_stop_invalid_selection(ids):
    with pytest.raises(ContainerError,match='valid container IDs'):
        ContainerRuntime('docker',SCOPE).stop(ids)


def test_stop_protects_session_before_engine():
    runtime=ContainerRuntime('docker',SCOPE)
    with patch('api_agent.runtime.containers.session_container_identity',return_value={'a'*64}), patch.object(runtime,'_run') as run:
        with pytest.raises(ContainerError,match='hosting'):
            runtime.stop(['a'*12])
        run.assert_not_called()


def test_cleanup_holds_lease_guard_through_inspect_and_stop(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from api_agent.runtime import prerequisites
    events = []
    @contextmanager
    def guard(directory, scope):
        assert directory == tmp_path and scope is SCOPE
        events.append('lock')
        yield frozenset()
        events.append('unlock')
    monkeypatch.setattr(prerequisites, 'guarded_cleanup', guard)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    runtime = ContainerRuntime('docker', SCOPE, ownership_directory=tmp_path)
    def run(arguments, **kwargs):
        events.append(arguments[0])
        assert events[0] == 'lock' and 'unlock' not in events
        return json.dumps([record()]) if arguments[0] == 'inspect' else ''
    monkeypatch.setattr(runtime, '_run', run)
    runtime.stop(['a' * 64])
    assert events == ['lock', 'inspect', 'stop', 'unlock']


def test_six_service_stop_is_bounded_and_checks_all_before_mutation(monkeypatch):
    ids = tuple(f'{index:064x}' for index in range(1, 7))
    records = [record(identifier) for identifier in ids]
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    runtime = ContainerRuntime('docker', SCOPE)
    with patch('api_agent.runtime.containers.subprocess.run', side_effect=[
            SimpleNamespace(returncode=0, stdout=json.dumps(records)),
            SimpleNamespace(returncode=0, stdout='')]) as run:
        runtime.stop(ids)
    assert run.call_args_list[0].args[0] == ['docker', 'inspect', *ids]
    assert run.call_args_list[1].args[0] == ['docker', 'stop', '--time', '10', *ids]
    assert run.call_args_list[1].kwargs['timeout'] == 77


def test_stop_timeout_reports_failure_after_full_reinspection(monkeypatch):
    ids = ('a' * 64, 'b' * 64)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    runtime = ContainerRuntime('docker', SCOPE)
    with patch('api_agent.runtime.containers.subprocess.run', side_effect=[
            SimpleNamespace(returncode=0, stdout=json.dumps([record(identifier) for identifier in ids])),
            subprocess.TimeoutExpired('docker stop', 29)]) as run:
        with pytest.raises(ContainerError, match='TimeoutExpired'):
            runtime.stop(ids)
    assert run.call_args_list[1].args[0] == ['docker', 'stop', '--time', '10', *ids]
    assert run.call_args_list[1].kwargs['timeout'] == 29


@pytest.mark.parametrize('uncertain', [False, True])
def test_cleanup_refuses_live_lease_or_unverifiable_journal(tmp_path, monkeypatch, uncertain):
    from contextlib import contextmanager
    from api_agent.runtime import prerequisites
    @contextmanager
    def guard(directory, scope):
        if uncertain:
            raise prerequisites.PrerequisiteError('private journal diagnostic')
        yield frozenset({'a' * 64})
    monkeypatch.setattr(prerequisites, 'guarded_cleanup', guard)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    runtime = ContainerRuntime('docker', SCOPE, ownership_directory=tmp_path)
    with patch.object(runtime, '_run') as run:
        with pytest.raises(ContainerError, match='cleanup refused' if uncertain else 'active invocation') as error:
            runtime.stop(['a' * 64])
        assert 'private' not in str(error.value)
        run.assert_not_called()


@pytest.mark.parametrize('response', ['bad', '{}', '[null]', '[{}]', json.dumps([record(Id='')]), json.dumps([record(Name=None)]), json.dumps([record(Config=None)]), json.dumps([record(State=None)])])
def test_invalid_inspect_response(response):
    runtime=ContainerRuntime('docker',SCOPE)
    with patch.object(runtime,'_run',return_value=response),pytest.raises(ContainerError,match='invalid engine response'):
        runtime._inspect(['id'])


def test_engine_errors_and_fallback():
    with patch('api_agent.runtime.containers.shutil.which',side_effect=[None,'podman']),patch('api_agent.runtime.containers.subprocess.run',return_value=SimpleNamespace(returncode=1)) as run:
        with pytest.raises(ContainerError,match='exit 1'):
            ContainerRuntime(scope=SCOPE).inventory()
        assert run.call_args.args[0][0]=='podman'
    with patch('api_agent.runtime.containers.shutil.which',return_value=None),pytest.raises(ContainerError,match='absent'):
        ContainerRuntime(scope=SCOPE).inventory()
    for error in [OSError(),subprocess.TimeoutExpired('docker',5)]:
        with patch('api_agent.runtime.containers.subprocess.run',side_effect=error),pytest.raises(ContainerError,match='unavailable'):
            ContainerRuntime('docker',SCOPE).inventory()


def test_metadata_errors_and_identity_matches():
    from api_agent.runtime.containers import session_container_identity, is_session_container
    with patch.dict('os.environ',{},clear=True),patch('api_agent.runtime.containers.Path.read_text',side_effect=FileNotFoundError):
        assert not session_container_identity()
    with patch('api_agent.runtime.containers.Path.read_text',side_effect=OSError),pytest.raises(ContainerError,match='identify'):
        session_container_identity()
    with patch('api_agent.runtime.containers.Path.read_text',return_value='id="bad'),pytest.raises(ContainerError,match='Invalid'):
        session_container_identity()
    assert is_session_container('abc', 'api', {'abcdef'})
    assert is_session_container('abcdef', 'api', {'abc'})
    assert is_session_container('other', '/api', {'api'})
    assert not is_session_container('other','api', {'different'})


def test_previous_scope_inventory_refuses_migration_without_stopping():
    legacy = 'b' * 16
    scope = StackScope(SCOPE.id, (legacy,))
    item = record()
    item['Config']['Labels']['api-agent.scope'] = legacy
    item['Config']['Labels']['com.docker.compose.project'] = 'sa-' + legacy
    with patch('api_agent.runtime.containers.subprocess.run', side_effect=[
        SimpleNamespace(returncode=0, stdout=item['Id']),
        SimpleNamespace(returncode=0, stdout=json.dumps([item]))]) as run:
        with pytest.raises(ContainerError, match='before migration'):
            ContainerRuntime('/bin/docker', scope).inventory()
        assert len(run.call_args_list) == 2
