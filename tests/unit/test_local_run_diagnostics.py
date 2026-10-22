"""Actual controller diagnostics retain registered secrets through failed setup."""
import asyncio
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from api_agent.runtime.local_terminal import run_local_terminal
from api_agent.tui.local_run import LocalRunScreen
from api_agent.testing.diagnostics import CredentialScrubber
from test_local_run import local

CONFIG_SECRET = 'synthetic-config-secret-01'
RUNTIME_SECRET = 'synthetic-runtime-secret-02'
MESSAGE = f'Selected UI readiness failed: {CONFIG_SECRET}, {RUNTIME_SECRET}\x1b[31m'


@pytest.mark.parametrize('consumer', ['terminal', 'tui'])
@pytest.mark.parametrize('phase', ['before-session', 'after-cleanup'])
def test_actual_controller_diagnostic_keeps_config_and_runtime_secrets_private(local, monkeypatch, consumer, phase):
    local.config.values.update(RUN_LOG_SOURCE='agent', API_KEY=CONFIG_SECRET)
    assert RUNTIME_SECRET in CredentialScrubber(local.config.values)(MESSAGE)
    async def failed_browser_start():
        local.session.runtime.emissions.register_secret(RUNTIME_SECRET)
        raise ValueError(MESSAGE)
    local.browser.start = failed_browser_start
    if phase == 'before-session':
        from api_agent.runtime import local_run, client_tls
        monkeypatch.setattr(local_run, 'compose_runtime', Mock(return_value=local.session.runtime))
        monkeypatch.setattr(client_tls, 'verified_client_context', lambda value: ssl.create_default_context())
        local.owner.llm_factory = Mock(return_value=SimpleNamespace(ensure_ready=AsyncMock(), describe=AsyncMock(), complete=AsyncMock(), aclose=AsyncMock()))
        @asynccontextmanager
        async def fail_before_session(**options):
            runtime = local.owner._runtime(local.config, 'https://localhost:8443', SimpleNamespace(ca_file=Path('/synthetic-ca')))
            runtime.emissions.register_secret(RUNTIME_SECRET)
            raise ValueError(MESSAGE)
            yield  # The context intentionally never yields an application session.
        local.operation.open = fail_before_session
    async def exercise():
        factory = lambda *args, **kwargs: local.owner
        if consumer == 'terminal':
            diagnostics = []
            status = await run_local_terminal(None, lambda: local.config, owner_factory=factory, diagnostic=diagnostics.append)
            text = '\n'.join(diagnostics)
        else:
            screen = LocalRunScreen(None, lambda: local.config, owner_factory=factory)
            await screen.run()
            status, text = screen.status, screen.failure
            await local.owner.aclose()
        assert status == 1
        assert 'Selected UI readiness failed' in text
        assert CONFIG_SECRET not in text
        assert RUNTIME_SECRET not in text
        assert '\x1b' not in text
        if phase == 'after-cleanup':
            assert local.calls[-2:] == ['browser-stop', 'operation-stop']
        else:
            local.browser_factory.assert_not_called()
    asyncio.run(exercise())
