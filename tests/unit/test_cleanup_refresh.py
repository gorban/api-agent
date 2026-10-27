"""Cleanup display reads coalesce and never publish obsolete source snapshots."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.tui.configuration_refresh import CleanupRefresher, fork_session
from test_tui import fixture


def state_for(app):
    screen = SimpleNamespace(query_one=Mock())
    state = SimpleNamespace(screen=screen, session=app.session,
                            cleanup_names=frozenset({'COUNT'}),
                            background_reads=app.background_reads,
                            apply_cleanup_rows=Mock())
    return state, screen


@pytest.mark.parametrize('change', ['dotenv', 'environ', 'choices', 'session'])
def test_cleanup_coalesces_navigation_and_delivers_only_current_values(tmp_path, monkeypatch, change):
    async def scenario():
        dotenv = tmp_path / '.env'
        dotenv.write_text('COUNT=3\n')
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
        state, screen = state_for(app)
        refresher = CleanupRefresher(state, screen)
        entered, release = threading.Event(), threading.Event()
        original = type(app.session).cleanup_rows
        calls = []

        def slow(session, **kwargs):
            rows = original(session, **kwargs)
            calls.append(rows)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return rows

        monkeypatch.setattr(type(app.session), 'cleanup_rows', slow)
        task = asyncio.create_task(refresher.run())
        assert await asyncio.to_thread(entered.wait, 5)
        if change == 'dotenv':
            dotenv.write_text('COUNT=9\n')
        elif change == 'environ':
            app.session.environ['COUNT'] = '9'
        elif change == 'choices':
            app.session._choices['COUNT'] = '9'
        else:
            state.session = fork_session(app.session)
            state.session.environ['COUNT'] = '9'
        for _ in range(12):
            refresher.invalidate()
        release.set()
        await task
        assert len(calls) == 2
        state.apply_cleanup_rows.assert_called_once()
        rows = state.apply_cleanup_rows.call_args.args[1]
        assert next(row for row in rows if row.name == 'COUNT').value == 9
        assert refresher.worker is None
    asyncio.run(scenario())


@pytest.mark.parametrize('transition', ['screen', 'modal', 'session', 'choices'])
def test_cleanup_pending_read_cannot_overwrite_a_newer_view(tmp_path, monkeypatch, transition):
    async def scenario():
        (tmp_path / '.env').write_text('COUNT=3\n')
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
        state, screen = state_for(app)
        refresher = CleanupRefresher(state, screen)
        original = refresher.read

        async def complete(_label, _read, session):
            result = original(session)
            if transition in {'screen', 'modal'}:
                state.screen = object()
            elif transition == 'session':
                state.session = fork_session(app.session)
            else:
                app.session._choices['COUNT'] = '9'
            return result

        monkeypatch.setattr(app.background_reads, 'run', complete)
        await refresher.run()
        state.apply_cleanup_rows.assert_not_called()
        assert refresher.worker is None
    asyncio.run(scenario())


def test_cleanup_retries_external_edit_without_another_navigation_event(tmp_path, monkeypatch):
    async def scenario():
        dotenv = tmp_path / '.env'
        dotenv.write_text('COUNT=3\n')
        app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
        state, screen = state_for(app)
        refresher = CleanupRefresher(state, screen)
        original = refresher.read
        calls = []

        async def complete(_label, _read, session):
            result = original(session)
            calls.append(True)
            if len(calls) == 1:
                dotenv.write_text('COUNT=9\n')
            return result

        monkeypatch.setattr(app.background_reads, 'run', complete)
        await refresher.run()
        assert len(calls) == 2
        rows = state.apply_cleanup_rows.call_args.args[1]
        assert next(row for row in rows if row.name == 'COUNT').value == 9
    asyncio.run(scenario())


def test_cleanup_snapshot_tracks_declaration_receipts(tmp_path, monkeypatch):
    from api_agent.config.source_reads import record_probe
    from api_agent.tui.configuration_refresh import read_cleanup_snapshot

    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
    owner = {'value': 1}
    original = type(app.session).cleanup_rows

    def read(session, **kwargs):
        record_probe('declaration', lambda: owner['value'], owner['value'])
        return original(session, **kwargs)

    monkeypatch.setattr(type(app.session), 'cleanup_rows', read)
    snapshot, current = read_cleanup_snapshot(fork_session(app.session), frozenset({'COUNT'}))
    assert current
    owner['value'] = 2
    assert not snapshot.sources_current()


def test_cleanup_navigation_requests_share_one_worker(tmp_path):
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
    state, screen = state_for(app)
    pending = []

    def worker(coroutine, **kwargs):
        pending.append(coroutine)
        return object()

    state.run_worker = worker
    refresher = CleanupRefresher(state, screen)
    for _ in range(12):
        refresher.request()
    assert len(pending) == 1
    assert refresher.generation == 12
    pending[0].close()


def test_cleanup_snapshot_uses_one_source_set_during_dotenv_aba_edit(tmp_path, monkeypatch):
    from api_agent.tui.configuration_refresh import read_cleanup_snapshot

    dotenv = tmp_path / '.env'
    dotenv.write_text('COUNT=3\n')
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 1}})
    original = type(app.session).cleanup_rows
    observed = []

    def edit_during_resolution(session, **kwargs):
        # The prepared input is A. Every independent reread during resolution
        # would instead observe B, before the source returns to A for delivery.
        dotenv.write_text('COUNT=9\n')
        observed.append(dotenv.read_text())
        try:
            return original(session, **kwargs)
        finally:
            dotenv.write_text('COUNT=3\n')

    monkeypatch.setattr(type(app.session), 'cleanup_rows', edit_during_resolution)
    snapshot, current = read_cleanup_snapshot(fork_session(app.session), frozenset({'COUNT'}))
    assert observed == ['COUNT=9\n']
    assert current
    assert dotenv.read_text() == 'COUNT=3\n'
    row = next(row for row in snapshot.rows if row.name == 'COUNT')
    assert row.value == 3
    assert row.present and row.base_present
