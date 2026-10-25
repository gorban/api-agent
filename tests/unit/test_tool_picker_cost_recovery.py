"""Conflicting test unions never retain or revive another selection's estimate."""
import threading
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Static

from api_agent.telemetry.estimation import EstimateSnapshot
from api_agent.tui.search import SearchInput
from test_tool_picker_conflicts import OTHER, conflicting_app
from test_tool_config_session import PRIMARY
from test_tool_selection import selection_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('search', [False, True])
async def test_cost_presentation_recovers_for_new_valid_selection(conflicting_app, monkeypatch, search):
    app = conflicting_app
    app.session.environ['API_ENDPOINT'] = '42'
    estimates = []

    def estimate(context, tests, values):
        assert app.session.tool_plan.selection.test_ids in ((PRIMARY,), (OTHER,))
        snapshot = EstimateSnapshot({PRIMARY: {'estimated_cost_usd': .42},
                                     OTHER: {'estimated_cost_usd': .84}})
        snapshot.synchronize = Mock(return_value=snapshot)
        estimates.append(snapshot)
        return snapshot

    build = Mock(side_effect=estimate)
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', build)
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.active_test_selectors = (PRIMARY,)
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert 'Selected estimate: $0.420000' in str(app.screen.query_one('#body', Static).render())
        assert len(app.screen.query_one(DataTable).columns) == 5
        assert len(estimates) == 1
        assert build.call_count == 1
        estimates[0].synchronize.assert_called_once_with()
        if search:
            await pilot.press('slash')
            app.screen.query_one(SearchInput).value = 'test_'
            await pilot.pause()

        app.screen.toggle_test_rows((OTHER,))
        await pilot.pause()
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert 'Selected estimate:' not in str(app.screen.query_one('#body', Static).render())
        assert len(app.screen.query_one(DataTable).columns) == 4
        assert len(estimates) == 1
        assert build.call_count == 1
        estimates[0].synchronize.assert_called_once_with()

        app.screen.toggle_test_rows((PRIMARY,))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not str(app.screen.query_one('#error', Static).render()).strip()
        assert app.selection.selected_ids == {OTHER}
        assert app.session.tool_plan.selection.test_ids == (OTHER,)
        assert len(estimates) == 2
        assert build.call_count == 2
        assert app.cost_estimator is estimates[1]
        estimates[1].synchronize.assert_called_once_with()
        assert len(app.screen.query_one(DataTable).columns) == 5
        assert 'Selected estimate: $0.840000' in str(app.screen.query_one('#body', Static).render())
        assert str(app.screen.query_one(DataTable).get_row(OTHER)[4]) == '0.840000'
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()


@pytest.mark.asyncio
async def test_failed_recovery_estimate_removes_previous_price(conflicting_app, monkeypatch):
    app = conflicting_app
    app.session.environ['API_ENDPOINT'] = '42'
    snapshot = EstimateSnapshot({PRIMARY: {'estimated_cost_usd': .42}})
    snapshot.synchronize = Mock(return_value=snapshot)
    build = Mock(return_value=snapshot)
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', build)
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.active_test_selectors = (PRIMARY,)
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        build.side_effect = OSError('synthetic cached history unavailable')
        app.screen.toggle_test_rows((OTHER,))
        app.screen.toggle_test_rows((PRIMARY,))
        await pilot.pause()

        assert app.session.tool_plan.selection.test_ids == (OTHER,)
        assert app.cost_estimator is None
        assert len(app.screen.query_one(DataTable).columns) == 4
        assert 'Selected estimate:' not in str(app.screen.query_one('#body', Static).render())
        assert not str(app.screen.query_one('#error', Static).render()).strip()
        assert build.call_count == 2
        snapshot.synchronize.assert_called_once_with()
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('transition', ['invalid', 'recovered', 'retired'])
async def test_late_preconflict_history_cannot_restore_retired_price(conflicting_app, monkeypatch, transition):
    import asyncio
    app = conflicting_app
    app.session.environ['API_ENDPOINT'] = '42'
    snapshots = []

    def estimate(context, tests, values):
        snapshot = EstimateSnapshot({PRIMARY: {'estimated_cost_usd': .42},
                                     OTHER: {'estimated_cost_usd': .84}})
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', estimate)
    entered, release = threading.Event(), threading.Event()
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            app.mode = 'integration'
            app.active_test_selectors = (PRIMARY,)
            app.collection_cancel = threading.Event()
            await app._collect_tests(app.collection_cancel)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            previous = app.cost_estimator

            def stale_history():
                entered.set()
                assert release.wait(5)
                return EstimateSnapshot({PRIMARY: {'estimated_cost_usd': 9.99},
                                         OTHER: {'estimated_cost_usd': 9.99}})

            previous.synchronize = stale_history
            stale = asyncio.create_task(app.synchronize_cost_history(previous, app.selection))
            assert await asyncio.to_thread(entered.wait, 5)
            app.screen.toggle_test_rows((OTHER,))
            if transition == 'recovered':
                app.screen.toggle_test_rows((PRIMARY,))
            elif transition == 'retired':
                app.show_menu()
            await pilot.pause()
            current = app.cost_estimator
            release.set()
            await stale
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.cost_estimator is current
            if transition == 'recovered':
                assert current is snapshots[1]
                assert 'Selected estimate: $0.840000' in str(app.screen.query_one('#body', Static).render())
            elif transition == 'invalid':
                assert current is None
                assert len(app.screen.query_one(DataTable).columns) == 4
                assert 'Selected estimate:' not in str(app.screen.query_one('#body', Static).render())
            else:
                assert app.screen.kind == 'menu'
            assert not app.test_run_active()
            app.integration_runner.assert_not_called()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_account_cycle_replaces_estimate_after_effective_change(tmp_path, monkeypatch):
    from api_agent.testing.cost_history import allocation_scope
    from test_account_selection import make_app
    app = make_app(tmp_path)
    (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
    built, scopes = [], []

    def estimate(context, tests, values):
        scopes.append(allocation_scope({**values, 'LLM_CREDENTIAL_ROLE': 'LLM User',
            '_COST_PROVIDER_IDENTITY': 'a' * 64, '_COST_PACK_IDENTITY': 'b' * 64,
            '_COST_REQUEST_SETTINGS': {}}))
        amount = .42 if values['UNATTENDED_USER_INDEX'] == 0 else .84
        snapshot = EstimateSnapshot({test.id: {'estimated_cost_usd': amount if i == 0 else 0}
                                     for i, test in enumerate(tests)})
        snapshot.synchronize = Mock(return_value=snapshot)
        built.append(snapshot)
        return snapshot

    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', estimate)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert 'Selected estimate: $0.420000' in str(app.screen.query_one('#body', Static).render())
        original = app.cost_estimator
        selected = app.selection.selected_ids.copy()

        await pilot.press('right_square_bracket')
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.session.result().values['UNATTENDED_USER_INDEX'] == 1
        assert app.session.saved_path('UNATTENDED_USER_INDEX') == tmp_path / '.env'
        assert 'second' in str(app.screen.query_one('#account-selection', Static).render())
        assert app.cost_estimator is not original
        assert len(built) == 2 and all(scopes) and scopes[0] != scopes[1]
        assert app.cost_estimator is built[1]
        assert built[1].picker_configuration['UNATTENDED_USER_INDEX'] == 1
        built[1].synchronize.assert_called_once_with()
        assert 'Selected estimate: $0.840000' in str(app.screen.query_one('#body', Static).render())
        assert app.selection.selected_ids == selected
        assert not app.test_run_active()


@pytest.mark.asyncio
async def test_cli_overridden_account_save_keeps_effective_estimate(tmp_path, monkeypatch):
    from test_account_selection import make_app
    app = make_app(tmp_path, argv=['--unattended-user-index=0'])
    snapshot = EstimateSnapshot({})
    snapshot.synchronize = Mock(return_value=snapshot)
    build = Mock(return_value=snapshot)
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', build)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('right_square_bracket')
        await pilot.pause()

        assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
        assert 'UNATTENDED_USER_INDEX="1"' in (tmp_path / '.env').read_text()
        assert 'cli overrides the saved choice' in str(app.screen.query_one('#error', Static).render())
        assert app.cost_estimator is snapshot
        build.assert_called_once()
        snapshot.synchronize.assert_called_once_with()
        assert not app.test_run_active()


@pytest.mark.asyncio
@pytest.mark.parametrize('overridden', [False, True])
async def test_account_cycle_preserves_invalid_plan_priority_without_history(conflicting_app, monkeypatch, overridden):
    import json
    from api_agent.config import schema_from_dict
    from test_account_selection import descriptors
    app = conflicting_app
    base = app.session._provider_base_schema
    additions = schema_from_dict({'version': 1, 'fields': {
        'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True,
                                  'validation': 'nonnegative', 'acknowledge_default': False},
        'UNATTENDED_AUTH': {'type': 'jsonobject', 'acknowledge_default': False}}})
    app.session.schema = type(base)({**base.fields, **additions.fields})
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(descriptors())
    (app.context.base_root / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
    if overridden:
        app.session.argv += ('--unattended-user-index=0',)
    build = Mock(side_effect=AssertionError('invalid plan touched cached history'))
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', build)
    async with app.run_test(size=(110, 40)) as pilot:
        app.mode = 'integration'
        app.collection_cancel = threading.Event()
        await app._collect_tests(app.collection_cancel)
        await pilot.pause()
        await pilot.press('right_square_bracket')
        await pilot.pause()

        assert app.session.result().values['UNATTENDED_USER_INDEX'] == (0 if overridden else 1)
        assert 'Conflicting configuration binding ownership' in str(app.screen.query_one('#error', Static).render())
        assert app.cost_estimator is None
        build.assert_not_called()
        assert not app.test_run_active()
        app.integration_runner.assert_not_called()


@pytest.mark.asyncio
async def test_account_change_clears_cost_when_new_estimate_unavailable(tmp_path, monkeypatch):
    from test_account_selection import make_app
    app = make_app(tmp_path)
    (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
    snapshot = EstimateSnapshot({})
    build = Mock(side_effect=[snapshot, OSError('synthetic account history unavailable')])
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', build)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('right_square_bracket')
        await pilot.pause()

        assert app.session.result().values['UNATTENDED_USER_INDEX'] == 1
        assert app.cost_estimator is None
        assert len(app.screen.query_one(DataTable).columns) == 4
        assert 'Selected estimate:' not in str(app.screen.query_one('#body', Static).render())
        assert build.call_count == 2
        assert not app.test_run_active()


@pytest.mark.asyncio
async def test_late_prior_account_history_cannot_restore_its_estimate(tmp_path, monkeypatch):
    import asyncio
    from test_account_selection import make_app
    app = make_app(tmp_path)
    (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=0\n')
    built = []

    def estimate(context, tests, values):
        amount = .42 if values['UNATTENDED_USER_INDEX'] == 0 else .84
        snapshot = EstimateSnapshot({test.id: {'estimated_cost_usd': amount if i == 0 else 0}
                                     for i, test in enumerate(tests)})
        built.append(snapshot)
        return snapshot

    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', estimate)
    entered, release = threading.Event(), threading.Event()
    try:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            previous = app.cost_estimator

            def previous_account_history():
                entered.set()
                assert release.wait(5)
                return EstimateSnapshot({test.id: {'estimated_cost_usd': 9.99} for test in app.selection.tests})

            previous.synchronize = previous_account_history
            stale = asyncio.create_task(app.synchronize_cost_history(previous, app.selection))
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.press('right_square_bracket')
            await app.workers.wait_for_complete()
            release.set()
            await stale
            await pilot.pause()

            assert app.cost_estimator is built[1] and app.cost_estimator is not previous
            assert app.cost_estimator.picker_configuration['UNATTENDED_USER_INDEX'] == 1
            assert 'Selected estimate: $0.840000' in str(app.screen.query_one('#body', Static).render())
            assert not app.test_run_active()
    finally:
        release.set()
