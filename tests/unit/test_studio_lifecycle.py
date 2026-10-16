"""Runtime retains the Studio backend's complete dependency tree on failure."""
import asyncio

import pytest

from api_agent.api import Runtime
from api_agent.providers.control import CleanupPendingError


def test_runtime_retains_backend_dependencies_until_retry_succeeds():
    async def run():
        closed = []
        class Backend:
            pending = True
            async def aclose(self):
                closed.append('backend')
                if self.pending:
                    raise CleanupPendingError((self,), self.aclose)
        class Resource:
            def __init__(self, name):
                self.name = name
            async def aclose(self):
                closed.append(self.name)
        backend = Backend()
        resources = {name: Resource(name) for name in
            ('sessions', 'identity_adapter', 'ui_provider', 'llm_provider', 'access_provider',
             'api_provider', 'upstream_provider', 'tracing')}
        runtime = Runtime('demo', services={'studio_backend': backend}, **resources)
        runtime.start()
        with pytest.raises(CleanupPendingError) as pending:
            await runtime.aclose()
        assert closed == ['backend']
        assert pending.value.owners == (backend, *resources.values())
        assert runtime.draining
        assert not runtime.ready
        backend.pending = False
        await pending.value.retry()
        assert closed == ['backend', 'backend', *resources]
        assert runtime.pending_cleanup is None
        await runtime.aclose()
        assert closed == ['backend', 'backend', *resources]
    asyncio.run(run())
