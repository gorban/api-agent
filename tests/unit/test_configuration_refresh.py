"""Display refresh stays isolated, coalesced and subordinate to fresh actions."""
import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable

from api_agent.tui.configuration_refresh import ReviewRefresher, controls, fork_session
from test_tui import fixture


def test_snapshot_projects_both_views_from_one_dotenv_read(tmp_path, monkeypatch):
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
    reads = []

    def read(path):
        reads.append(path)
        return {'COUNT': '7' if len(reads) == 1 else '9'}

    monkeypatch.setattr('api_agent.config.session.read_dotenv', read)
    snapshot = app.session.review_snapshot()
    assert reads == [tmp_path / '.env']
    assert snapshot.execution.values['COUNT'] == snapshot.review.values['COUNT'] == 7
    assert snapshot.saved_names == {'COUNT'}


def test_fork_keeps_live_resolver_and_definition_eviction_independent(tmp_path):
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
    original = app.session
    before = controls(original)
    fork = fork_session(original)
    fork.environ['COUNT'] = '8'
    fork._choices['COUNT'] = '9'
    fork._definition_cache.max_entries = 0
    snapshot = fork.review_snapshot()
    assert snapshot.review.values['COUNT'] == 9
    assert controls(original) == before
    assert original.result().values['COUNT'] == 3
    assert original._definition_cache.max_entries == 32
    assert original._definition_cache is not fork._definition_cache
    assert original._definition_cache._lock is not fork._definition_cache._lock


@pytest.mark.parametrize('change', ['dotenv', 'choices', 'environ', 'session'])
def test_pending_refresh_coalesces_and_discards_stale_external_values(tmp_path, monkeypatch, change):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        screen = SimpleNamespace(query_one=Mock())
        state = SimpleNamespace(screen=screen, session=app.session,
                                apply_configuration_snapshot=Mock())
        refresher = ReviewRefresher(state, screen)
        entered, release = threading.Event(), threading.Event()
        original = type(app.session).review_snapshot
        calls = []

        def slow(session):
            snapshot = original(session)
            calls.append(snapshot)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return snapshot

        monkeypatch.setattr(type(app.session), 'review_snapshot', slow)
        task = asyncio.create_task(refresher.run())
        assert await asyncio.to_thread(entered.wait, 5)
        if change == 'dotenv':
            (tmp_path / '.env').write_text('COUNT=9\n')
        elif change == 'choices':
            app.session._choices['COUNT'] = '9'
        elif change == 'environ':
            app.session.environ['COUNT'] = '9'
        else:
            state.session = fork_session(app.session)
            state.session.environ['COUNT'] = '9'
        for _ in range(12):
            refresher.invalidate()
        release.set()
        await task
        assert len(calls) == 2
        state.apply_configuration_snapshot.assert_called_once()
        applied = state.apply_configuration_snapshot.call_args.args[1]
        assert applied.review.values['COUNT'] == 9
        assert applied.saved_names == ({'COUNT'} if change == 'dotenv' else set())
        assert refresher.worker is None
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['screen', 'session', 'choices', 'error', 'stale_error'])
def test_late_or_failed_refresh_cannot_overwrite_newer_ui(tmp_path, monkeypatch, change):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        screen = SimpleNamespace(query_one=Mock())
        state = SimpleNamespace(screen=screen, session=app.session,
                                apply_configuration_snapshot=Mock())
        refresher = ReviewRefresher(state, screen)

        async def complete(*_args):
            if change in {'screen', 'stale_error'}:
                state.screen = object()
            elif change == 'session':
                state.session = fork_session(app.session)
            elif change == 'choices':
                app.session._choices['COUNT'] = '4'
            if change in {'error', 'stale_error'}:
                raise ValueError('private details')
            return object()

        monkeypatch.setattr('api_agent.tui.configuration_refresh.asyncio.to_thread', complete)
        await refresher.run()
        state.apply_configuration_snapshot.assert_not_called()
        if change == 'error':
            screen.query_one.return_value.update.assert_called_once_with(
                'Configuration refresh failed; move to retry.')
        else:
            screen.query_one.assert_not_called()
    asyncio.run(scenario())


def test_single_worker_request_and_inactive_screen(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        state = SimpleNamespace(screen=object(), session=app.session, run_worker=Mock(return_value=object()))
        refresher = ReviewRefresher(state, object())
        refresher.request()
        refresher.request()
        assert refresher.generation == 2
        state.run_worker.assert_called_once()
        # The mock did not schedule the coroutine.
        await state.run_worker.call_args.args[0]
        assert refresher.worker is None
    asyncio.run(scenario())


def test_cleanup_return_selects_last_successful_deletion(tmp_path, monkeypatch):
    async def scenario():
        (tmp_path / '.env').write_text('B=1\nC=2\n')
        app = fixture(tmp_path, fields={name: {'type': 'int', 'default': 3} for name in ('A', 'B', 'C')})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.show_cleanup()
            await pilot.pause()
            app.delete_assignment('B')
            app.delete_assignment('C')
            monkeypatch.setattr(app.session, 'delete_keys', Mock(side_effect=OSError('failed write')))
            app.delete_assignment('B')
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.focused_setting() == 'C'
            app.show_cleanup()
            await pilot.pause()
            await app.close_view()
            await pilot.pause()
            assert app.focused_setting() == 'A'
    asyncio.run(scenario())


def test_hidden_deleted_setting_falls_back_to_first_review_row(tmp_path):
    async def scenario():
        (tmp_path / '.env').write_text('HIDDEN=1\n')
        app = fixture(tmp_path, fields={'A': {'type': 'int', 'default': 3},
                                       'HIDDEN': {'type': 'int', 'default': 4, 'acknowledge_default': False}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.show_cleanup()
            await pilot.pause()
            app.delete_assignment('HIDDEN')
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.focused_setting() == 'A'
    asyncio.run(scenario())


def test_review_cursor_moves_while_configuration_read_is_blocked(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, fields={name: {'type': 'int', 'default': 3} for name in ('A', 'B', 'C')})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            if getattr(app.screen, 'configuration_refresher', None) and app.screen.configuration_refresher.worker:
                await app.screen.configuration_refresher.worker.wait()
            entered, release = threading.Event(), threading.Event()
            original = type(app.session).review_snapshot
            main_thread = threading.current_thread()

            def slow(session):
                snapshot = original(session)
                if threading.current_thread() is not main_thread:
                    entered.set()
                    assert release.wait(5)
                return snapshot

            monkeypatch.setattr(type(app.session), 'review_snapshot', slow)
            table = app.screen.query_one(DataTable)
            rows = []
            app.watch(table, 'cursor_coordinate', lambda point: rows.append(point.row), init=False)
            try:
                await pilot.press('down')
                assert await asyncio.to_thread(entered.wait, 5)
                async with asyncio.timeout(3):
                    await pilot.press('down', 'up', 'up')
                assert rows == [1, 2, 1, 0]
                assert not release.is_set()
            finally:
                release.set()
            await app.screen.configuration_refresher.worker.wait()
    asyncio.run(scenario())


@pytest.mark.parametrize('retained_hidden', [False, True])
def test_edit_revalidates_a_setting_removed_after_display(tmp_path, retained_hidden):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            # Simulate a fresh provider schema dropping the displayed setting.
            from api_agent.config import schema_from_dict
            fields = {'OTHER': {'type': 'int', 'default': 9}}
            if retained_hidden:
                fields['COUNT'] = {'type': 'int', 'default': 3, 'acknowledge_default': False}
            schema = schema_from_dict({'version': 1, 'fields': fields})
            app.session.schema = schema
            app.edit_setting()
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.focused_setting() == 'OTHER'
            assert app.session.tool_plan is None
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['read', 'handoff'])
@pytest.mark.parametrize('navigate', [False, True])
@pytest.mark.parametrize('replacement', ['# empty\n', 'COUNT=9\n'])
def test_pending_review_applies_external_value_source_and_unset_together(tmp_path, monkeypatch, navigate, replacement, stage):
    async def scenario():
        path = tmp_path / '.env'
        path.write_text('COUNT=3\n')
        app = fixture(tmp_path, fields={name: {'type': 'int', 'default': 3} for name in ('A', 'COUNT')})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.review_names.add('COUNT')
            app.show_configuration()
            await pilot.pause()
            if getattr(app.screen, 'configuration_refresher', None) and app.screen.configuration_refresher.worker:
                await app.screen.configuration_refresher.worker.wait()
            entered, release = threading.Event(), threading.Event()
            original = type(app.session).review_snapshot
            captured = []

            def slow(session):
                snapshot = original(session)
                captured.append(snapshot)
                if len(captured) == 1 and stage == 'read':
                    entered.set()
                    assert release.wait(5)
                return snapshot

            monkeypatch.setattr(type(app.session), 'review_snapshot', slow)
            if stage == 'handoff':
                import api_agent.tui.configuration_refresh as module
                original_read = module.read_snapshot
                def delayed_handoff(session):
                    value = original_read(session)
                    if len(captured) == 1:
                        entered.set()
                        assert release.wait(5)
                    return value
                monkeypatch.setattr(module, 'read_snapshot', delayed_handoff)
            table = app.screen.query_one(DataTable)
            try:
                await pilot.press('down')
                assert await asyncio.to_thread(entered.wait, 5)
                assert table.get_row_at(1)[1] == '3'
                assert app.screen.check_action('unset_setting', ())
                stamp = path.stat()
                path.write_text(replacement)  # Same byte count; restore timestamps too.
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                if navigate:
                    await pilot.press('up', 'down')
            finally:
                release.set()
            await app.screen.configuration_refresher.worker.wait()
            assert len(captured) == 2
            if replacement.startswith('#'):
                assert table.get_row_at(1) == ['COUNT', '3', 'shared_default']
                assert not app.screen.check_action('unset_setting', ())
            else:
                assert table.get_row_at(1) == ['COUNT', '9', app.session._source(path)]
                assert app.screen.check_action('unset_setting', ())
    asyncio.run(scenario())


def test_continuously_changed_sources_stop_after_bounded_attempts(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        screen = SimpleNamespace(query_one=Mock())
        state = SimpleNamespace(screen=screen, session=app.session,
                                apply_configuration_snapshot=Mock())
        from api_agent.config.session import ReviewSnapshot
        current = Mock(return_value=False)
        monkeypatch.setattr(ReviewSnapshot, 'sources_current', current)
        refresher = ReviewRefresher(state, screen)
        await refresher.run()
        assert current.call_count == 3
        state.apply_configuration_snapshot.assert_not_called()
        screen.query_one.return_value.update.assert_called_once_with(
            'Configuration sources keep changing; move to retry.')
    asyncio.run(scenario())


def test_newer_request_gets_its_own_source_retry_budget(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        screen = SimpleNamespace(query_one=Mock())
        state = SimpleNamespace(screen=screen, session=app.session,
                                apply_configuration_snapshot=Mock())
        refresher = ReviewRefresher(state, screen)
        from api_agent.config.session import ReviewSnapshot
        calls = []
        def changing():
            calls.append(True)
            if len(calls) == 2:
                refresher.invalidate()
            return False
        monkeypatch.setattr(ReviewSnapshot, 'sources_current', Mock(side_effect=changing))
        await refresher.run()
        assert len(calls) == 5
        state.apply_configuration_snapshot.assert_not_called()
        screen.query_one.return_value.update.assert_called_once_with(
            'Configuration sources keep changing; move to retry.')
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['resolution', 'delivery'])
def test_transient_source_read_failure_is_retried(tmp_path, monkeypatch, where):
    async def scenario():
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 3}})
        screen = SimpleNamespace(query_one=Mock())
        state = SimpleNamespace(screen=screen, session=app.session,
                                apply_configuration_snapshot=Mock())
        from api_agent.config.session import ReviewSnapshot
        if where == 'resolution':
            original = type(app.session).review_snapshot
            attempts = []
            def read(session):
                attempts.append(True)
                if len(attempts) == 1:
                    raise OSError('transient')
                return original(session)
            monkeypatch.setattr(type(app.session), 'review_snapshot', read)
        else:
            monkeypatch.setattr(ReviewSnapshot, 'sources_current', Mock(
                side_effect=[True, OSError('transient'), True, True]))
        await ReviewRefresher(state, screen).run()
        state.apply_configuration_snapshot.assert_called_once()
        screen.query_one.assert_not_called()
    asyncio.run(scenario())
