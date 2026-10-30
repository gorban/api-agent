"""Registry loss during a real authority handoff fails closed."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from api_agent.providers import local_call_registry as calls
from api_agent.providers import setup_transport as transport
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope


@pytest.mark.parametrize('stage', ['before-transfer', 'after-transfer', 'saturated-fault'])
@pytest.mark.parametrize('replace', [False, True])
def test_registry_identity_change_rejects_handoff(stage, replace, monkeypatch):
    secret = 'synthetic-local-handoff-credential'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', boundary)
    dispatched, releases, replacements, server_errors = [], [], [], []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            dispatched.append(request)
            return LLMResult(Message('assistant', (TextContent('done'),)))

    def call(descriptor):
        client = SetupClient(descriptor, timeout=5)
        original_release = client._accept_authority_release
        def release(frame):
            original_release(frame)
            releases.append(frame)
        client._accept_authority_release = release
        try:
            with pytest.raises(ContractError) as error:
                client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                                authority_scope=scope)
            assert secret not in str(error.value)
        finally:
            client.close()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=boundary)
        descriptor = await server.start()
        original_dispatch = server._dispatch_captured
        original_caller = server._caller_authority
        identity = None
        entry = None
        def change():
            calls.remove(identity, entry)
            if replace:
                replacements.append((identity, calls.register(identity, scope)))
        async def dispatch(peer, frame, action, collector, early_result, early_error, local_identity, gated_entry):
            nonlocal identity, entry
            if action == 'complete':
                identity, entry = local_identity, gated_entry
                assert entry is not None
                if stage == 'before-transfer':
                    change()
            try:
                return await original_dispatch(peer, frame, action, collector, early_result, early_error,
                                               local_identity, gated_entry)
            except ContractError as error:
                if action == 'complete':
                    server_errors.append(str(error))
                raise
        async def caller(peer, frame):
            result = await original_caller(peer, frame)
            if stage == 'saturated-fault':
                # Fault injection models registry loss between the two checks;
                # the saturated fast path itself does not yield to the event loop.
                assert result is None and peer.authority_ticket is None
            else:
                assert result is not None and peer.authority_ticket is not None
            change()
            return result
        monkeypatch.setattr(server, '_dispatch_captured', dispatch)
        if stage != 'before-transfer':
            monkeypatch.setattr(server, '_caller_authority', caller)
        if stage == 'saturated-fault':
            # Only the count is read on the capacity-rejection path.
            server._authority_pending.update({str(i): None for i in range(transport._MAX_SETUP_PEERS)})
        try:
            await asyncio.wait_for(asyncio.to_thread(call, descriptor), 10)
            assert not dispatched
            assert server_errors == ['Caller authority changed during admission']
            assert len(releases) == (1 if stage == 'after-transfer' else 0)
            assert secret not in repr(releases)
            assert scope._leases == 0
        finally:
            server._authority_pending.clear()
            for registration in replacements:
                calls.remove(*registration)
            await server.aclose()
            await registry.aclose()
            scope.close()
    asyncio.run(scenario())
