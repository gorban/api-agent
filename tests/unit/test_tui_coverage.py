"""Boundary, navigation and shutdown behavior with deterministic adapters."""
import asyncio
from types import SimpleNamespace
import threading

import pytest
from rich.text import Text
from textual.events import Key
from textual.widgets import Button, DataTable, Input, Static
from textual.containers import VerticalScroll

from test_tui import fixture
from api_agent.testing.collection import TestIdentity as Identity
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.selection import SelectionModel
from api_agent.tui.app import ApiAgentApp, WorkbenchScreen, display, run_tui
from api_agent.tui.test_results import reconcile_results_table, styled_summary


def run(scenario):
    asyncio.run(scenario())


def failure(message):
    def raise_error(*args, **kwargs):
        raise ValueError(message)
    return raise_error


def identity(name='a'):
    return Identity(f'base::test.py::test_{name}', 'base', 'test.py', f'test.py::test_{name}')


def test_presentation_reconcile_and_summary_preserve_precise_colors(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            app.show('test-results', 'Results', rows=[('a', ('PASSED', 'a', '1s'))])
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.clear()
            reconcile_results_table(table, [('b', ('FAILED', 'b', '2s'))], completed=True)
            assert table.get_row_at(0) == ['FAILED', 'b', '2s']
            reconcile_results_table(table, [('b', ('PASSED', 'b', '3s'))], completed=False)
            assert table.get_row_at(0) == ['PASSED', 'b', '3s']
    run(scenario)
    assert display([1, 2]) == '[1,2]'
    summary = styled_summary(Text('PASS\nCoverage: partial\nProduction coverage: 100%'), failed=False,
                             neutral_details=True, green_prefixes=('Production coverage:',))
    assert summary.get_style_at_offset(__import__('rich').console.Console(), 0).color.name == 'green'
    assert summary.get_style_at_offset(__import__('rich').console.Console(), 5).color is None
    assert 'green' in str(summary.spans[-1].style)


def test_container_inventory_and_stop_errors_are_visible_and_recoverable(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await app.choose('mode-cleanup-containers')
            await pilot.pause()
            await app.choose('stop-containers')
            assert 'at least one' in str(app.screen.query_one('#error', Static).render())
            app.container_runtime = SimpleNamespace(inventory=failure('Container engine refused connection'), stop=failure('stop failed'))
            await app._refresh_container_inventory()
            await pilot.pause()
            assert 'refused' in app.container_summary
            app.container_runtime = SimpleNamespace(inventory=failure('private engine detail'), stop=failure('stop failed'))
            await app._refresh_container_inventory()
            await pilot.pause()
            assert app.container_summary == 'Container inventory unavailable.'
            item = SimpleNamespace(id='id', name='name', image='image', status='running')
            app.container_runtime = SimpleNamespace(inventory=lambda: (item,), stop=failure('stop failed'))
            await app._refresh_container_inventory()
            await pilot.pause()
            app.toggle_row('id')
            app.toggle_row('container-all')
            assert not app.container_marked
            app.toggle_row('container-all')
            assert app.container_marked == {'id'}
            app.toggle_row('unknown')
            await app._stop_containers((item,))
            await pilot.pause()
            assert app.operation_status == 3
            assert 'stop failed' in str(app.screen.query_one('#error', Static).render())
            await app.choose('refresh-containers')
            await pilot.pause()
            await app.choose('stop-containers')
            await pilot.pause()
            await pilot.click('#stop-cancel')
            await pilot.pause()
            assert app.containers == (item,)
            app.show_menu()
            await pilot.pause()
            app.show_container_stop_error('obsolete error')
            assert app.screen.kind == 'menu'
            app.show('unavailable', 'Another view')
            await pilot.pause()
            app.update_container_menu()
            await app._refresh_container_inventory()
            await pilot.pause()
            assert app.screen.kind == 'unavailable'
    run(scenario)


def test_configuration_action_errors_keep_editable_rows(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 4}})
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(app.session, 'set_value', failure('write failed'))
            await app.accept_default()
            assert 'write failed' in str(app.screen.query_one('#error', Static).render())
            monkeypatch.setattr(app.session, 'unset_saved', failure('unset failed'))
            app.screen.action_unset_setting()
            assert 'unset failed' in str(app.screen.query_one('#error', Static).render())
            monkeypatch.setattr(app.session, 'delete_keys', failure('delete failed'))
            app.delete_assignment('COUNT')
            assert 'delete failed' in str(app.screen.query_one('#error', Static).render())
            app.show_menu()
            await pilot.pause()
            await app.accept_default()
            app.edit_setting()
            assert app.screen.kind == 'menu'
            assert not app.focused_cleanup_present()
            app.show('defaults', 'Empty checklist', rows=[('COUNT', ('COUNT', '4', 'default'))])
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.clear()
            assert app.focused_setting() is None
            app.edit_setting()
            await app.accept_default()
            assert app.screen.kind == 'defaults'
            app.screen.action_delete_assignment()
    run(scenario)


def test_collection_errors_cancellation_and_selection_refresh(tmp_path):
    async def scenario():
        app = fixture(tmp_path, collector=lambda *args: [])
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.back_to_selection()
            app.mode = 'unit'
            await app._collect_tests(threading.Event())
            await pilot.pause()
            assert app.screen.heading == 'Collection failed'
            app.collector = failure('collection import failed')
            await app._collect_tests(threading.Event())
            await pilot.pause()
            assert 'import failed' in str(app.screen.query_one('#body', Static).render())
            stopped = threading.Event()
            stopped.set()
            await app._collect_tests(stopped)
            await pilot.pause()
            assert app.screen.kind == 'collecting'
            await app.close_view()
            await pilot.pause()
            assert app.collection_cancel.is_set()
            app.selection = SelectionModel([identity()])
            app.collector = failure('refresh failed')
            await app.back_to_selection()
            await pilot.pause()
            assert app.screen.heading == 'Collection failed'
            def cancel_refresh(*args):
                app.collection_cancel.set()
                return [identity()]
            app.collector = cancel_refresh
            await app.back_to_selection()
            await pilot.pause()
            assert app.screen.kind == 'collecting'
            def cancel_error(*args):
                app.collection_cancel.set()
                raise ValueError('cancelled failure')
            app.collector = cancel_error
            await app.back_to_selection()
            await pilot.pause()
            assert app.screen.kind == 'collecting'
            await app._collect_tests(app.collection_cancel)
            await pilot.pause()
            assert app.screen.kind == 'collecting'
            app.test_run = TestRunLifecycle()
            await app.back_to_selection()
            app.test_run.finish()
    run(scenario)


def test_run_tui_forwards_console_outputs_and_unknown_return(monkeypatch, capsys):
    for result, expected in [(0, 0), (130, 130), (None, 3)]:
        app = SimpleNamespace(run=lambda: result, console_error='configuration error', collected_output='test identity')
        monkeypatch.setattr('api_agent.tui.app.ApiAgentApp', lambda *args: app)
        assert run_tui(None, None) == expected
        captured = capsys.readouterr()
        assert captured.out == 'test identity\n'
        assert captured.err == 'configuration error\n'
    monkeypatch.setattr('api_agent.tui.app.ApiAgentApp', lambda *args: SimpleNamespace(run=lambda: 0))
    assert run_tui(None, None) == 0


def test_real_green_execution_from_picker_and_completed_nonfailure_row(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/green_fixture.py').write_text('VALUE = 1\n')
    (tmp_path / 'tests/unit').mkdir(parents=True)
    (tmp_path / 'tests/unit/test_green.py').write_text('from green_fixture import VALUE\ndef test_green():\n    assert VALUE == 1\n')
    async def scenario():
        app = fixture(tmp_path, 'unit', fields={'TEST_PARALLEL_JOBS': {'type': 'int', 'default': 0, 'acknowledge_default': False, 'persist_default': False}})
        app.context.owner_root = tmp_path
        app.context.suite_paths = lambda mode: [tmp_path / 'tests/unit']
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            deadline = asyncio.get_running_loop().time() + 15
            while app.screen.kind != 'tests' and asyncio.get_running_loop().time() < deadline:
                await pilot.pause(.03)
            assert app.screen.kind == 'tests'
            await pilot.press('s')
            while app.unit_result is None and asyncio.get_running_loop().time() < deadline:
                await pilot.pause(.03)
            assert app.unit_result is not None
            await pilot.pause()
            assert app.operation_status == 0
            assert app.unit_result.require_full_coverage is True
            assert app.unit_result.coverage
            assert all(area.complete for area in app.unit_result.coverage)
            assert app.test_run.stopped.is_set()
            assert not app.test_run.active
            assert app.unit_result.tests[0].status == 'passed'
            assert '100%' in app.screen.query_one('#body', Static).content.plain
            app.toggle_row(app.unit_result.tests[0].identity.id)
            assert app.screen.kind == 'test-results'
    run(scenario)


@pytest.mark.parametrize('condition', ['empty', 'error', 'cancel', 'cancel_error'])
def test_unit_recollection_stops_or_reports_actionable_error(tmp_path, condition):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'unit'
            app.selection = SelectionModel([identity()])
            app.test_run = TestRunLifecycle()
            app.collection_cancel = app.test_run.cancel_event
            app.unit_rows = {identity().id: ('PENDING', identity().id, '')}
            def collector(*args):
                if condition.startswith('cancel'):
                    app.collection_cancel.set()
                if condition.endswith('error'):
                    raise ValueError('recollection failed')
                return []
            app.collector = collector
            await app._run_units()
            await pilot.pause()
            assert not app.test_run.active
            if condition in {'empty', 'error'}:
                assert app.screen.heading == 'Unit execution failed'
                assert app.operation_status == 3
            else:
                assert app.unit_result is None
    run(scenario)


def test_quit_waits_for_active_run_and_retains_interrupt_status(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'unit'
            app.unit_rows = {'a': ('RUNNING', 'a', '')}
            app.test_run = TestRunLifecycle()
            stopping = asyncio.create_task(app.confirm_quit())
            await asyncio.sleep(0)
            assert app.test_run.cancel_event.is_set()
            assert not stopping.done()
            assert app.operation_status == 130
            app.test_run.finish()
            await stopping
            assert app.return_value == 130
    run(scenario)


def test_navigation_setup_acceptance_and_menu_routes(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show('suggest', 'Setup')
            await pilot.pause()
            await app.choose('setup')
            await pilot.pause()
            assert app.screen.kind == 'unavailable'
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('skip')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-unattended-auth')
            await pilot.pause()
            assert app.screen.kind == 'unavailable'
            await app.choose('menu')
            await pilot.pause()
            assert app.mode is None and app.screen.kind == 'menu'
            app.mode = 'integration'
            app.selection = SelectionModel([identity()])
            app.show_selection()
            await pilot.pause()
            await app.choose('continue')
            await pilot.pause()
            assert 'Integration execution' in str(app.screen.query_one('#body', Static).render())
            app.mode = 'e2e'
            app.show_selection()
            await pilot.pause()
            await app.choose('continue')
            await pilot.pause()
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind == 'tests'
            await app.choose('continue')
            await pilot.pause()
            await app.choose('decline')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            await app.choose('unknown')
            assert app.screen.kind == 'tests'
            await app.choose('exit')
            assert app.return_value == 3
    run(scenario)


def test_late_events_and_refresh_callbacks_cannot_mutate_replaced_screen(tmp_path):
    from api_agent.testing.runner import RunnerEvent
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.unit_event(RunnerEvent('output', text='raw output'))
            app.unit_event(RunnerEvent('test_started', identity=identity()))
            assert app.unit_rows[identity().id][0] == 'RUNNING'
            app.collection_cancel.set()
            app.unit_event(RunnerEvent('test_finished', identity=identity(), status='passed'))
            assert app.unit_rows[identity().id][0] == 'RUNNING'
            app.collection_cancel.clear()
            app.show_unit_results('Running')
            await pilot.pause()
            old = app.screen
            app.unit_event(RunnerEvent('test_finished', identity=identity(), status='passed', duration=.5))
            assert old.query_one(DataTable).get_row_at(0)[0] == 'PASSED'
            app.show_menu()
            await pilot.pause()
            app.update_test_results(old)
            app.style_unit_summary()
            old.layout_test_results()
            assert app.screen.kind == 'menu'
            app.unit_rows.clear()
            app.show_unit_results('Nothing running')
            await pilot.pause()
            await app.close_view()
            assert app.screen.kind == 'test-results'
            app.update_test_results(app.screen)
            app.screen.layout_test_results()
    run(scenario)


def test_failure_scroll_bindings_and_navigation_ignore_empty_tables(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show('test-failure', 'Failure', '\n'.join(str(i) for i in range(100)))
            await pilot.pause()
            screen = app.screen
            pane = screen.query_one(VerticalScroll)
            pane.focus()
            screen.on_key(Key('j', 'j'))  # active failure binding owns this key
            await pilot.press('down', 'pagedown', 'pageup', 'up')
            screen.action_failure_down()
            screen.action_failure_page_down()
            await pilot.pause()
            assert pane.scroll_y > 0
            screen.action_failure_up()
            screen.action_failure_page_up()
            await pilot.pause()
            app.show('tests', 'Empty selector', rows=[('a', ('', 'a', '0/1', 'test'))])
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.styles.overflow_x = "hidden"
            app.screen.on_key(Key('h', 'h'))
            table.clear()
            app.screen.action_toggle_selection()
            assert table.row_count == 0
    run(scenario)


def test_container_success_confirmation_and_keyboard_stop(tmp_path):
    async def scenario():
        stopped = []
        item = SimpleNamespace(id='owned', name='fixture', image='fixture:image', status='running')
        inventory = [item]
        def stop(ids):
            stopped.extend(ids)
            inventory.clear()
        app = fixture(tmp_path)
        app.container_runtime = SimpleNamespace(inventory=lambda: tuple(inventory), stop=stop)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await app.choose('mode-cleanup-containers')
            await pilot.pause()
            app.toggle_row('container-all')
            await pilot.press('s')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#stop-submit')
            await pilot.pause()
            assert stopped == ['owned'] and app.operation_status == 0
            assert app.containers == ()
    run(scenario)


def test_configuration_empty_auth_invalid_startup_and_collect_mode_errors(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int'}})
        app.session.environ['COUNT'] = 'invalid'
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.heading == 'Configuration error'
    run(scenario)
    async def mode_error():
        app = fixture(tmp_path, fields={'COLLECT_ONLY': {'type': 'bool', 'default': False, 'acknowledge_default': False, 'persist_default': False}})
        app.session.environ['COLLECT_ONLY'] = 'True'
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('mode-run-local')
            await pilot.pause()
            assert app.screen.heading == 'Invalid collection mode'
    run(mode_error)
    async def auth_required():
        app = fixture(tmp_path, fields={'UNATTENDED_AUTH': {'type': 'jsonobject', 'required': True}})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.environ.pop('UNATTENDED_AUTH')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.heading == 'Unattended authentication required'
    run(auth_required)


def test_context_target_switches_preserve_view_and_configuration_empty_fallback(tmp_path):
    from api_agent.config.session import ConfigSession
    async def scenario():
        base, pack = tmp_path / 'base', tmp_path / 'pack'
        base.mkdir()
        pack.mkdir()
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 4}})
        app.context.base_root, app.context.pack_root = base, pack
        app.context.external_pack = True
        app.context.dotenv_path = pack / '.env'
        app.session = ConfigSession(app.context, app.schema, environ=app.session.environ)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show_cleanup()
            await pilot.pause()
            app.switch_target()
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            app.unavailable('Wizard pending')
            await pilot.pause()
            app.switch_target()
            assert app.screen.kind == 'unavailable'
            app.show_configuration()
            await pilot.pause()
            app.edit_setting()
            await pilot.pause()
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            app.review_names.clear()
            app.session.set_value('COUNT', '4')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.kind == 'menu'
    run(scenario)


def test_accept_default_no_stdin_restarts_without_followup_prompts(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'NO_STD_IN': {'type': 'bool', 'default': True}})
        async with app.run_test() as pilot:
            await pilot.pause()
            # Startup exits as soon as the interactive configuration acknowledges
            # NO_STD_IN; accepting the displayed default uses the same exit path.
            assert app.screen.kind == 'defaults'
            await app.accept_default()
            assert app.return_value == 2
            assert 'Restart' in app.console_error
    run(scenario)


def test_conditional_default_disappears_when_no_fallback_is_declared(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={
            'ENABLED': {'type': 'bool', 'default': True},
            'OPTIONAL': {'type': 'str', 'conditional_defaults': [{'when': {'ENABLED': True}, 'value': 'present'}]},
        })
        async with app.run_test() as pilot:
            await pilot.pause()
            assert 'OPTIONAL' in [app.screen.query_one(DataTable).get_row_at(i)[0] for i in range(2)]
            app.session.set_value('ENABLED', 'False')
            app.show_configuration()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1 and table.get_row_at(0)[0] == 'ENABLED'
    run(scenario)


def test_successful_collection_after_cancel_never_opens_stale_picker(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            event = threading.Event()
            def collect(*args):
                event.set()
                return [identity()]
            app.collector = collect
            app.mode = 'unit'
            await app._collect_tests(event)
            await pilot.pause()
            assert app.screen.kind == 'collecting'
    run(scenario)


def test_menu_request_while_active_requires_stop_handshake(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.test_run = TestRunLifecycle()
            await app.choose('menu')
            await pilot.pause()
            assert app.screen.__class__.__name__ == 'QuitDialog'
            await pilot.click('#quit-cancel')
            await pilot.pause()
            assert app.test_run.active
            app.test_run.finish()
    run(scenario)


def test_cleanup_removed_last_hidden_setting_handles_empty_snapshot(tmp_path):
    async def scenario():
        (tmp_path / '.env').write_text('HIDDEN=present\n')
        app = fixture(tmp_path, 'config-cleanup', fields={'HIDDEN': {'type': 'str', 'acknowledge_default': False, 'persist_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1
            app.session.delete_keys(['HIDDEN'])
            app.refresh_rows()
            assert table.row_count == 0
            assert not app.focused_cleanup_present()
            # An already-delivered inventory row event remains harmless after
            # the inventory snapshot has emptied.
            app.show('containers', 'Empty containers', rows=[('old', ('', 'old', 'name', 'image', 'running'))])
            await pilot.pause()
            app.containers = ()
            app.toggle_row('old')
            assert app.container_marked == set()
    run(scenario)
