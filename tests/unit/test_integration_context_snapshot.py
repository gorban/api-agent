"""Untrusted snapshot paths must be rejected before any blocking IPC read."""
import os
import subprocess
import sys
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('kind', ['fifo', 'directory'])
def test_non_regular_snapshot_is_rejected_without_blocking(tmp_path, kind):
    path = tmp_path / 'context'
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o600)
    code = '''
from api_agent.providers.contracts import ContractError
from api_agent.testing.provider_setup import _read_snapshot
try:
    _read_snapshot()
except ContractError as error:
    assert str(error) == 'Integration provider context is unavailable; start through launch.sh.'
else:
    raise AssertionError('Non-regular context was accepted')
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            env={**os.environ, 'API_AGENT_INTEGRATION_CONTEXT': str(path)},
                            timeout=3)
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_load_owners_are_closed_before_retrying_setup(tmp_path, monkeypatch, cancelled):
    from api_agent.providers.loading import ProviderError
    from api_agent.testing import provider_setup as setup
    retained = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('cleanup pending'), None]))
    error = asyncio.CancelledError() if cancelled else ProviderError('provider_cleanup_pending')
    error.retained_owners = (retained,)
    class Auth:
        async def __call__(self): return object()
        async def aclose(self): pass
    provider = SimpleNamespace(implementation=SimpleNamespace(asetup=AsyncMock(return_value=Auth()),
                               credential_protection_version=1, bind_credential_authority=lambda authority: None),
                               aclose=AsyncMock())
    load = AsyncMock(side_effect=[error, provider])
    monkeypatch.setattr(setup, 'aload_provider', load)
    instance = setup.IntegrationLLM({'LLM_PROVIDER': 'selected'}, tmp_path)
    try:
        with pytest.raises(type(error)):
            instance.ensure_ready()
        assert instance._retained_owners == [retained]
        with pytest.raises(RuntimeError, match='cleanup pending'):
            instance.ensure_ready()
        assert load.await_count == 1 and instance._retained_owners == [retained]
        instance.ensure_ready()
        assert retained.aclose.await_count == 2 and load.await_count == 2
        assert instance._retained_owners == []
    finally:
        instance.close()
    assert instance.loop.is_closed() and provider.aclose.await_count == 1


def test_failed_load_owners_remain_owned_until_close_succeeds(tmp_path, monkeypatch):
    from api_agent.providers.loading import ProviderError
    from api_agent.testing import provider_setup as setup
    retained = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('cleanup pending'), None]))
    monkeypatch.setattr(setup, 'aload_provider', AsyncMock(side_effect=ProviderError(
        'provider_cleanup_pending', retained_owners=(retained,))))
    instance = setup.IntegrationLLM({'LLM_PROVIDER': 'selected'}, tmp_path)
    with pytest.raises(ProviderError):
        instance.ensure_ready()
    with pytest.raises(RuntimeError, match='cleanup pending'):
        instance.close()
    assert not instance.closed and not instance.loop.is_closed()
    assert instance._retained_owners == [retained]
    instance.close()
    assert instance.closed and instance.loop.is_closed()
    assert instance._retained_owners == [] and retained.aclose.await_count == 2


def test_real_validation_rollback_is_retried_before_loop_closes(tmp_path):
    from api_agent.providers.loading import ProviderError
    from api_agent.testing.provider_setup import IntegrationLLM
    factory = tmp_path / 'factory.py'
    factory.write_text('''class Invalid:
    version = 2
    def __init__(self): self.close_attempts = 0
    async def aclose(self):
        self.close_attempts += 1
        if self.close_attempts == 1: raise RuntimeError('cleanup pending')
def factory(config, services): return Invalid()
''')
    instance = IntegrationLLM({'LLM_PROVIDER': str(factory) + ':factory'}, tmp_path)
    with pytest.raises(ProviderError) as caught:
        instance.ensure_ready()
    retained = caught.value.retained_owners[0]
    assert retained.implementation.close_attempts == 1 and not retained.disposed
    instance.close()
    assert retained.implementation.close_attempts == 2 and retained.disposed
    assert instance.loop.is_closed()
