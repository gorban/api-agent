"""CLI recovery components share the async caller's deadline/progress scope."""
import asyncio
import threading
from unittest.mock import patch

import pytest

from api_agent.providers import progress


def test_sync_component_budget_inheritance_and_reset(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(progress, '_clock', lambda: now[0])
    events = []
    with progress.progress_sink(events.append):
        with progress.report_timeout_sync('Parent', 10) as parent:
            now[0] += 4
            with progress.report_timeout_sync('Retrieval', 15) as child:
                assert child.parent_id == parent.id
                assert child.timeout == 6 and child.remaining == 6
            now[0] += 6
            with progress.report_timeout_sync('Login', None) as login:
                assert login.remaining == 0 and login.timeout == 0
    assert [(e.name, e.active) for e in events] == [
        ('Parent', True), ('Retrieval', True), ('Retrieval', False),
        ('Login', True), ('Login', False), ('Parent', False)]
    assert events[3].timeout is None
    with progress.report_timeout_sync('No sink', None) as budget:
        assert budget.deadline is None and budget.remaining is None
    with pytest.raises(ValueError):
        with progress.report_timeout_sync('Bad', -1):
            pass
    with pytest.raises(ValueError):
        with progress.report_timeout_sync('', 1):
            pass


def test_cli_thread_uses_async_parent_and_observer_failures_are_inert():
    async def scenario():
        events = []
        def cli():
            with progress.report_timeout_sync('Retrieval', 15) as budget:
                assert budget.parent_id == parent.id
                assert 0 < budget.remaining <= 1
                return budget.id
        with progress.progress_sink(events.append):
            async with progress.report_timeout('Async parent', 1) as parent:
                child_id = await asyncio.to_thread(cli)
        assert any(e.id == child_id and not e.active for e in events)
    asyncio.run(scenario())
    def broken(event):
        raise RuntimeError('observer disconnected')
    with progress.progress_sink(broken):
        with progress.report_timeout_sync('Inert observer', 1):
            pass


def test_sync_heartbeat_and_exception_finalization(monkeypatch):
    events = []
    original = threading.Event
    class FastEvent:
        def __init__(self):
            self.real = original()
            self.ticks = 0
        def wait(self, seconds):
            self.ticks += 1
            if self.ticks == 1:
                return False
            return self.real.wait(seconds)
        def set(self):
            self.real.set()
    with patch.object(progress.threading, 'Event', FastEvent):
        # Thread itself also uses Event, so create that thread before patching.
        thread = threading.Thread
        with patch.object(progress.threading, 'Thread') as factory:
            def start(**kwargs):
                with patch.object(progress.threading, 'Event', original):
                    return thread(**kwargs)
            factory.side_effect = start
            with progress.progress_sink(events.append):
                with pytest.raises(RuntimeError, match='body error'):
                    with progress.report_timeout_sync('CLI', 1):
                        raise RuntimeError('body error')
    assert [e.active for e in events] == [True, True, False]
    assert progress._scope.get() is None
