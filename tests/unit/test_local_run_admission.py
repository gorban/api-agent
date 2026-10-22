"""Presentation joins consume controller admission without starting rejected runs."""
import asyncio

import pytest

from api_agent.runtime.local_terminal import run_local_terminal
from test_local_run import local


@pytest.mark.parametrize('source', ['1', 'missing-service'])
def test_terminal_rejected_source_has_no_operation_effects(local, source):
    local.config.values['RUN_LOG_SOURCE'] = source
    diagnostics = []
    async def exercise():
        status = await run_local_terminal(None, lambda: local.config,
            owner_factory=lambda *a, **k: local.owner,
            diagnostic=diagnostics.append,
            output=lambda text: pytest.fail('rejected source emitted run logs'))
        assert status != 0
        local.operation_factory.assert_not_called()
    asyncio.run(exercise())
    assert diagnostics
