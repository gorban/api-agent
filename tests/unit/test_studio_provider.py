"""Included frontend launch plans use the runtime lifecycle owner."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api_agent.providers.contracts import SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError
from api_agent.ui import provider


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize('value', ['invalid', 'https://example.com', 'http://localhost/x', 'http://localhost:99999', 'http://localhost:0', 'https://127.0.0.1:0', None, 'http://u:p@localhost', 'http://localhost?q=1', 'http://localhost#x'])
def test_invalid_listener(value):
    with pytest.raises(ValueError, match='ui_listener_invalid'):
        provider.listener_plan(value)


def test_listener_and_resources(tmp_path, monkeypatch):
    assert provider.listener_plan('http://localhost')[1] == 80
    assert provider.listener_plan('https://localhost')[1] == 443
    assert provider.app_directory().name == 'api-agent-ui'
    monkeypatch.setattr(provider, '__file__', str(tmp_path / 'provider.py'))
    (tmp_path / 'app').mkdir()
    assert provider.app_directory() == tmp_path / 'app'


def test_provider_lifecycle(monkeypatch):
    process = SimpleNamespace(aclose=AsyncMock())
    launcher = SimpleNamespace(start=AsyncMock(return_value=process))
    monkeypatch.setattr(provider.shutil, 'which', lambda _: '/usr/bin/node')
    instance = provider.create_provider({}, {'ui_processes':launcher, 'ui_access_token':'a' * 32, 'tls_material':SimpleNamespace(cert_file='cert', key_file='key')})
    async def exercise():
        await instance.astart()
        await instance.astart()
        assert launcher.start.await_count == 1
        args = launcher.start.call_args
        assert Path(args.args[0][1]).name == 'launch.mjs'
        assert args.kwargs['env']['API_AGENT_UI_TLS_KEY'] == 'key'
        assert args.kwargs['env']['API_AGENT_UI_ACCESS_TOKEN'] == 'a' * 32
        assert args.kwargs['scheme'] == 'https'
        await instance.aclose()
        await instance.aclose()
        process.aclose.assert_awaited_once()
        with pytest.raises(ValueError, match='ui_provider_closed'):
            await instance.astart()
    run(exercise())


@pytest.mark.parametrize('missing', ['service', 'node', 'resources', 'tls'])
def test_missing_prerequisites(missing, monkeypatch, tmp_path):
    services = {'ui_processes':SimpleNamespace(start=AsyncMock())}
    if missing == 'service': services.clear()
    monkeypatch.setattr(provider.shutil, 'which', lambda _: None if missing == 'node' else '/usr/bin/node')
    if missing == 'resources': monkeypatch.setattr(provider, 'app_directory', lambda:tmp_path)
    with pytest.raises(SetupPrerequisiteError): run(provider.create_provider({}, services).astart())


@pytest.mark.parametrize('cancel', [False, True])
def test_pending_cleanup_retry(cancel, monkeypatch):
    monkeypatch.setattr(provider.shutil, 'which', lambda _: '/usr/bin/node')
    pending = CleanupPendingError((), AsyncMock())
    error = asyncio.CancelledError() if cancel else pending
    if cancel: error.cleanup_pending = pending
    launcher = SimpleNamespace(start=AsyncMock(side_effect=error))
    instance = provider.create_provider({'UI_LISTENER_URL':'http://localhost:9443'}, {'ui_processes':launcher})
    async def exercise():
        with pytest.raises(type(error)): await instance.astart()
        assert instance.pending is pending
        launcher.start.side_effect = None
        launcher.start.return_value = SimpleNamespace(aclose=AsyncMock())
        await instance.astart()
        assert instance.pending is None
        instance.pending = pending
        await instance.aclose()
    run(exercise())
