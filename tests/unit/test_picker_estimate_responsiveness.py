"""History projection must not hold keyboard input or revive old accounts."""
import asyncio
import threading
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable
from textual.worker import WorkerCancelled

from api_agent.telemetry.estimation import EstimateSnapshot
from test_account_selection import make_app
from test_tool_picker_conflicts import conflicting_app
from test_tool_selection import selection_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['latest', 'leave', 'failure', 'source', 'cancel', 'malformed'])
async def test_slow_account_estimates_keep_input_live_and_discard_stale_results(tmp_path, monkeypatch, finish):
    app = make_app(tmp_path)
    (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
    entered, release = threading.Event(), threading.Event()
    snapshots, indexes = [], []
    main_thread = threading.get_ident()

    def estimate(context, tests, values, **options):
        index = values['UNATTENDED_USER_INDEX']
        indexes.append(index)
        if len(indexes) == 2:
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(5)
            if finish == 'failure':
                raise OSError('synthetic history unavailable')
        snapshot = EstimateSnapshot({test.id: {'estimated_cost_usd': .42} for test in tests})
        snapshot.close = Mock()
        snapshot.synchronize = Mock(return_value=snapshot)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', estimate)
    try:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            select_context = Mock(wraps=app.select_tool_context)
            monkeypatch.setattr(app, 'select_tool_context', select_context)
            await pilot.press('right_square_bracket')
            select_context.assert_not_called()
            assert await asyncio.to_thread(entered.wait, 2)
            table = app.screen.query_one(DataTable)
            original_row = table.cursor_row
            await pilot.press('down')
            assert table.cursor_row == original_row + 1
            assert app.cost_estimator is None
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 1

            if finish == 'latest':
                await pilot.press('left_square_bracket')
                await pilot.press('space')
                assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
                assert len(indexes) == 2  # one worker, despite repeated requests
            elif finish == 'cancel':
                app.screen.picker_estimates.worker.cancel()
                await asyncio.sleep(0)
            elif finish == 'malformed':
                (tmp_path / '.env').write_text('invalid assignment\n')
            elif finish == 'source':
                (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
            elif finish == 'leave':
                app.show_menu()
                await pilot.pause()
            release.set()
            if finish == 'cancel':
                with pytest.raises(WorkerCancelled):
                    await app.workers.wait_for_complete()
            else:
                await app.workers.wait_for_complete()
            await app.background_reads.close(lambda message: None)
            await pilot.pause()

            if finish in {'latest', 'source'}:
                assert indexes == [0, 1, 0]
                snapshots[1].close.assert_called_once_with()
                snapshots[1].synchronize.assert_not_called()
                assert app.cost_estimator is snapshots[2]
                assert app.cost_estimator.picker_configuration['UNATTENDED_USER_INDEX'] == 0
            elif finish == 'leave':
                assert app.screen.kind == 'menu'
                snapshots[1].close.assert_called_once_with()
                snapshots[1].synchronize.assert_not_called()
                assert app.cost_estimator is None
            elif finish in {'cancel', 'malformed'}:
                assert app.cost_estimator is None
                snapshots[1].close.assert_called_once_with()
                snapshots[1].synchronize.assert_not_called()
            else:
                assert app.cost_estimator is None
                assert indexes == [0, 1]
    finally:
        release.set()


def test_estimator_read_tracks_the_selected_provider_profile_source(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.tui.picker_estimates import PickerEstimates
    from api_agent.tui.test_configuration import _profile_revision
    factory = tmp_path / 'provider.py'
    profile = tmp_path / 'test-profiles.toml'
    profile.write_text('version=1\n')
    result = SimpleNamespace(values={'LLM_PROVIDER': str(factory) + ':factory'})
    review = SimpleNamespace(execution=result)
    context = SimpleNamespace(owner_root=tmp_path)
    session = SimpleNamespace(context=context, review_snapshot=Mock(return_value=review))
    app = SimpleNamespace(context=context, _cost_history_generation=0)
    selection = SimpleNamespace(tests=('synthetic-test',))
    snapshot = EstimateSnapshot({})
    build = Mock(return_value=snapshot)
    monkeypatch.setattr('api_agent.tui.picker_estimates.build_estimator', build)
    monkeypatch.setattr('api_agent.providers.loading.provider_location', lambda *args, **kwargs: (factory, 'factory'))
    reader = PickerEstimates(app, object(), selection)
    actual, receipt, source, before = reader.read(session)
    assert actual is snapshot and receipt is review and source == profile
    build.assert_called_once_with(context, selection.tests, result)
    assert _profile_revision(source) == before
    profile.write_text('version=2\n')
    assert _profile_revision(source) != before


@pytest.mark.asyncio
async def test_changing_sources_stop_after_three_disposed_projections(tmp_path):
    from types import SimpleNamespace
    from api_agent.tui.background_reads import BackgroundReads
    from api_agent.tui.picker_estimates import PickerEstimates
    original = make_app(tmp_path)
    screen, selection = object(), object()
    app = SimpleNamespace(_cost_history_generation=0, screen_stack=[screen], selection=selection,
        test_run_active=lambda: False, _inspection_shutdown=False, session=original.session,
        background_reads=BackgroundReads(), cost_estimator=None)
    reader = PickerEstimates(app, screen, selection)
    snapshots = [EstimateSnapshot({}) for _ in range(3)]
    for snapshot in snapshots:
        snapshot.close = Mock()
    review = SimpleNamespace(sources_current=lambda: False)
    reader.read = Mock(side_effect=[(snapshot, review, None, None) for snapshot in snapshots])
    await reader.run()
    assert reader.read.call_count == 3
    assert app.cost_estimator is None and reader.worker is None
    for snapshot in snapshots:
        snapshot.close.assert_called_once_with()
    app.screen_stack.clear()
    await reader.run()
    assert reader.read.call_count == 3


@pytest.mark.asyncio
async def test_cancelled_or_failed_read_disposal_has_no_snapshot_to_close():
    from api_agent.tui.picker_estimates import PickerEstimates
    cancelled = asyncio.get_running_loop().create_future()
    cancelled.cancel()
    failed = asyncio.get_running_loop().create_future()
    error = OSError('synthetic read failure')
    failed.set_exception(error)
    PickerEstimates.discard(cancelled)
    PickerEstimates.discard(failed)
    assert cancelled.cancelled() and failed.exception() is error


def test_account_summary_without_a_screen_reads_fresh_unpersisted_choices(tmp_path):
    app = make_app(tmp_path)
    assert 'No unattended user set' in str(app.account_summary())
    app.session.set_value('UNATTENDED_USER_INDEX', '1')
    assert 'second' in str(app.account_summary())
    assert not (tmp_path / '.env').exists()


@pytest.mark.asyncio
async def test_recovery_projection_cannot_revive_an_invalid_tool_union(conflicting_app, monkeypatch):
    from test_tool_picker_conflicts import OTHER
    from test_tool_config_session import PRIMARY
    app = conflicting_app
    app.session.environ['API_ENDPOINT'] = '42'
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    def estimate(context, tests, values, **options):
        if snapshots:
            entered.set()
            assert release.wait(5)
        snapshot = EstimateSnapshot({PRIMARY: {'estimated_cost_usd': .42},
                                     OTHER: {'estimated_cost_usd': .84}})
        snapshot.close = Mock()
        snapshot.synchronize = Mock(return_value=snapshot)
        snapshots.append(snapshot)
        return snapshot
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', estimate)
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            app.mode = 'integration'
            app.active_test_selectors = (PRIMARY,)
            app.collection_cancel = threading.Event()
            await app._collect_tests(app.collection_cancel)
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.screen.toggle_test_rows((OTHER,))
            app.screen.toggle_test_rows((PRIMARY,))
            assert await asyncio.to_thread(entered.wait, 2)
            app.screen.toggle_test_rows((PRIMARY,))
            assert app.screen.configuration_error
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.cost_estimator is None
            assert len(snapshots) == 2
            snapshots[1].close.assert_called_once_with()
            snapshots[1].synchronize.assert_not_called()
    finally:
        release.set()



@pytest.mark.asyncio
async def test_control_change_after_read_discards_the_stale_result_before_retry(tmp_path):
    from types import SimpleNamespace
    from api_agent.tui.background_reads import BackgroundReads
    from api_agent.tui.picker_estimates import PickerEstimates
    original = make_app(tmp_path)
    screen, selection = object(), object()
    async def synchronize(*args):
        return None
    app = SimpleNamespace(_cost_history_generation=0, screen_stack=[screen], selection=selection,
        test_run_active=lambda: False, _inspection_shutdown=False, session=original.session,
        background_reads=BackgroundReads(), cost_estimator=None, refresh_picker_costs=Mock(),
        synchronize_cost_history=synchronize, run_worker=Mock(side_effect=lambda work, **kwargs: work.close()))
    reader = PickerEstimates(app, screen, selection)
    first, latest = EstimateSnapshot({}), EstimateSnapshot({})
    first.close, latest.close = Mock(), Mock()
    snapshots = iter((first, latest))
    def read(session, cancel):
        snapshot = next(snapshots)
        if snapshot is first:
            app.session.environ['SYNTHETIC_CONTROL'] = 'changed after capture'
        return snapshot, SimpleNamespace(sources_current=lambda: True), None, None
    reader.read = Mock(side_effect=read)
    await reader.run()
    assert reader.read.call_count == 2
    first.close.assert_called_once_with()
    latest.close.assert_not_called()
    assert app.cost_estimator is latest
    app.refresh_picker_costs.assert_called_once_with(screen, selection)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['all', 'file:base::t.py', 'base::t.py::a'])
async def test_picker_toggle_reuses_fresh_selected_context_for_account_label(tmp_path, monkeypatch, key):
    from textual.widgets import Static
    app = make_app(tmp_path)
    snapshot = EstimateSnapshot({})
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', Mock(return_value=snapshot))
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.session.environ['UNATTENDED_USER_INDEX'] = '1'
        previous = app.selection.selected_ids.copy()
        normal_result = type(app.session).result
        ui_resolutions = []
        def result(session, *args, **kwargs):
            if threading.get_ident() == main_thread:
                ui_resolutions.append(kwargs)
                assert not kwargs.get('_review'), 'toggle added a synchronous account review'
            return normal_result(session, *args, **kwargs)
        main_thread = threading.get_ident()
        monkeypatch.setattr(type(app.session), 'result', result)

        app.screen.toggle_test_rows((key,))

        assert ui_resolutions == [{}]  # the normal selected-union validation only
        assert app.selection.selected_ids != previous
        assert app.session._tool_selection == tuple(app.selection.selected_tests)
        assert 'second' in str(app.screen.query_one('#account-selection', Static).content)
        assert app.screen.account_cycle_available
        assert not app.screen.configuration_error
        assert not (tmp_path / '.env').exists()


@pytest.mark.asyncio
async def test_invalid_toggle_keeps_changed_dotenv_account_current_without_an_added_review(conflicting_app, monkeypatch):
    import json
    from api_agent.config import schema_from_dict
    from textual.widgets import Static
    from test_account_selection import descriptors
    from test_tool_config_session import PRIMARY
    from test_tool_picker_conflicts import OTHER
    app = conflicting_app
    app.session.environ['API_ENDPOINT'] = '42'
    base = app.session._provider_base_schema
    additions = schema_from_dict({'version': 1, 'fields': {
        'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True, 'validation': 'nonnegative'},
        'UNATTENDED_AUTH': {'type': 'jsonobject', 'acknowledge_default': False}}})
    app.session.schema = type(base)({**base.fields, **additions.fields})
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(descriptors())
    dotenv = app.context.base_root / '.env'
    dotenv.write_text('UNATTENDED_USER_INDEX=0\n')
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', Mock(return_value=EstimateSnapshot({})))
    async with app.run_test(size=(110, 40)) as pilot:
        app.mode = 'integration'
        app.active_test_selectors = (PRIMARY,)
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert 'first' in str(app.screen.query_one('#account-selection', Static).content)
        normal_result = type(app.session).result
        main_thread = threading.get_ident()
        ui_resolutions = []
        def result(session, *args, **kwargs):
            if threading.get_ident() == main_thread:
                ui_resolutions.append(kwargs)
                assert not kwargs.get('_review'), 'failed union added a synchronous account review'
            return normal_result(session, *args, **kwargs)
        monkeypatch.setattr(type(app.session), 'result', result)

        dotenv.write_text('UNATTENDED_USER_INDEX=1\n')
        app.screen.toggle_test_rows((OTHER,))

        assert app.screen.configuration_error
        assert app.cost_estimator is None
        assert 'second' in str(app.screen.query_one('#account-selection', Static).content)
        assert ui_resolutions == [{}, {}]  # rejected union and its existing collection fallback
        dotenv.write_text('UNATTENDED_USER_INDEX=0\n')
        app.screen.toggle_test_rows((OTHER,))
        assert not app.screen.configuration_error
        assert app.session.tool_plan.selection.test_ids == (PRIMARY,)
        assert 'first' in str(app.screen.query_one('#account-selection', Static).content)
        assert ui_resolutions == [{}, {}, {}]
        app.integration_runner.assert_not_called()


def test_selected_context_still_raises_the_original_union_error_after_fallback(tmp_path):
    from types import SimpleNamespace
    from api_agent.tooling.models import ToolSelectionError
    app = make_app(tmp_path)
    error, fallback = ToolSelectionError('synthetic selected-union conflict'), object()
    select = Mock(side_effect=[error, fallback])
    app.session = SimpleNamespace(select_operation=select)

    with pytest.raises(ToolSelectionError) as caught:
        app.select_tool_context(('synthetic-test',))

    assert caught.value is error
    assert select.call_args_list[0].args == (('synthetic-test',),)
    assert select.call_args_list[1].args[0].mode == 'collection'
