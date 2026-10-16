"""UI browsers use the shared end_inspection protocol and their creating loop."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.testing import provider_setup


@pytest.mark.parametrize('inspection_present,session_present,events', [(False, False, False), (True, False, True), (True, True, False)])
def test_unretained_ui_inspection_closes_loop(inspection_present, session_present, events, monkeypatch):
    if events:
        monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', '/unused/events')
    else:
        monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    retained = []
    monkeypatch.setattr(provider_setup, '_inspection_providers', retained)
    inspector = SimpleNamespace(sessions=[object()] if session_present else [],
        wait_closed=AsyncMock(), aclose=AsyncMock()) if inspection_present else None
    loop = asyncio.new_event_loop()
    owner = provider_setup.finish_ui_browser_inspection(loop, inspector)
    assert owner.closed and loop.is_closed()
    assert not retained
    owner.close()
    if inspector is not None:
        inspector.wait_closed.assert_awaited_once()
        inspector.aclose.assert_awaited_once()


def test_allocated_ui_inspection_reuses_shared_end_command_and_cleanup_retry(tmp_path, monkeypatch):
    retained = []
    monkeypatch.setattr(provider_setup, '_inspection_providers', retained)
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    command = tmp_path / 'inspection-command.json'
    command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 1}))
    session = SimpleNamespace(inspection_endpoint='http://127.0.0.1:1234',
        browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()]))
    attempts = 0
    async def close():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
            raise RuntimeError('controlled cleanup pending')
    inspector = SimpleNamespace(sessions=[session], aclose=close, wait_closed=AsyncMock())
    loop = asyncio.new_event_loop()
    owner = provider_setup.finish_ui_browser_inspection(loop, inspector)
    assert retained == [owner]
    assert not loop.is_closed()
    assert hash(owner)
    emitted = []
    provider_setup.serve_inspections(lambda **event: emitted.append(event))
    assert attempts == 2
    assert owner.closed and loop.is_closed()
    assert emitted == [
        {'kind': 'inspection_open', 'endpoint': 'http://127.0.0.1:1234'},
        {'kind': 'inspection_cleanup_pending', 'attempt': 1},
        {'kind': 'inspection_closed', 'attempt': 2}]


@pytest.mark.parametrize('owned_task', [False, True])
def test_shared_end_inspection_retains_native_work_and_complete_loop_shutdown(tmp_path, monkeypatch, owned_task):
    import threading
    from api_agent.runtime.tasks import offload
    retained = []
    monkeypatch.setattr(provider_setup, '_inspection_providers', retained)
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    command = tmp_path / 'inspection-command.json'
    command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 1}))
    release, entered, finished, generator_closed = (threading.Event() for _ in range(4))
    def native():
        entered.set()
        release.wait(10)
        finished.set()
    loop = asyncio.new_event_loop()
    loop_errors = []
    loop.set_exception_handler(lambda loop, context: loop_errors.append(context))
    async def generator():
        try:
            yield 1
        finally:
            generator_closed.set()
    stream = generator()
    async def open_stream():
        return await anext(stream)
    loop.run_until_complete(open_stream())
    if owned_task:
        task = loop.create_task(offload(native))
    else:
        task = loop.run_in_executor(None, native)
    async def started():
        while not entered.is_set():
            await asyncio.sleep(.01)
    loop.run_until_complete(started())
    session = SimpleNamespace(inspection_endpoint='http://127.0.0.1:1234',
        browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()]))
    inspector = SimpleNamespace(sessions=[session], aclose=AsyncMock())
    owner = provider_setup.finish_ui_browser_inspection(loop, inspector)
    events = []
    def send(**event):
        events.append(event)
        if event['kind'] == 'inspection_cleanup_pending':
            assert not owner.closed
            assert not loop.is_closed()
            assert not finished.is_set()
            release.set()
            command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
    from api_agent.providers.progress import progress_sink
    progress = []
    try:
        with progress_sink(progress.append):
            provider_setup.serve_inspections(send)
        assert not loop_errors
        assert progress and progress[-1].active is False
        assert finished.is_set() and task.done()
        assert generator_closed.is_set()
        assert owner._shutdown.done()
        assert owner.closed and loop.is_closed()
        assert [event['kind'] for event in events] == ['inspection_open', 'inspection_cleanup_pending', 'inspection_closed']
    finally:
        release.set()
        if not loop.is_closed():
            loop.run_until_complete(owner.dispose_inspection())
            loop.close()
