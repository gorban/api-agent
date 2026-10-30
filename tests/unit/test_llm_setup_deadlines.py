"""Local deadlines stay recognizable across authentication and IPC boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.providers.setup_transport import _error, _raise_error


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['setup', 'refresh', 'provider'])
async def test_local_deadline_preserves_safe_stage_and_classification_over_wire(phase):
    auth = SimpleNamespace(aclose=AsyncMock())
    async def refresh():
        if phase == 'refresh':
            from api_agent.providers.progress import component_timeout
            import asyncio
            async with component_timeout('Synthetic authentication refresh', .001):
                await asyncio.Event().wait()
        return auth
    class Authentication:
        __call__ = staticmethod(refresh)
        aclose = auth.aclose
    setup = AsyncMock(return_value=Authentication())
    operation = AsyncMock(return_value=())
    if phase != 'refresh':
        target = setup if phase == 'setup' else operation
        target.side_effect = TimeoutError('unsafe synthetic-secret https://example.test/private')
    owner = LLMSessionOwner(SimpleNamespace(asetup=setup))
    try:
        with pytest.raises(ProviderAccessError) as caught:
            await owner.call(operation)
        frame = _error(caught.value)
        with pytest.raises(ProviderAccessError) as wire:
            _raise_error(frame)
        assert wire.value.kind == 'deadline'
        assert wire.value.execution == ('possibly_executed' if phase == 'provider' else 'not_started')
        assert ('provider operation' if phase == 'provider' else 'authentication setup') in str(wire.value)
        assert 'timed out' in str(wire.value)
        assert 'synthetic-secret' not in str(frame) and 'example.test' not in str(frame)
        if phase != 'provider':
            operation.assert_not_awaited()
        else:
            operation.assert_awaited_once()
    finally:
        await owner.aclose()
