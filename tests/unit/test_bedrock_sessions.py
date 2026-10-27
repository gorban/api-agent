import asyncio
import pytest
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial, SessionRequest
from api_agent.llm_providers.bedrock.sessions import AcquiredSession
from api_agent.providers.contracts import ContractError, SessionClosedError


class Adapter:
    def __init__(self):
        self.calls = []
        self.material = SessionMaterial('profile')
        self.error = False
        self.close_error = False
        self.closed = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def acquire(self, request, *, previous=None):
        self.calls.append((request, previous))
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise RuntimeError('secret adapter message')
        return self.material

    async def aclose(self):
        self.closed += 1
        if self.close_error:
            raise RuntimeError('secret close message')


def session(adapter, now=None):
    return AcquiredSession(adapter, SessionRequest('profile', 'region', None, False), clock=lambda: now[0] if now is not None else 0)


def temp(expiry):
    return SessionMaterial('temporary', access_key='key', secret_key='secret', session_token='token', expires_at=expiry)


@pytest.mark.asyncio
async def test_unacquired_profile_and_close():
    adapter = Adapter()
    owner = session(adapter)
    assert repr(owner) == '<AcquiredSession>'
    with pytest.raises(ContractError, match='not been acquired'):
        owner.worker_config()
    assert await owner() is owner
    result = owner.worker_config()
    result.clear()
    assert await owner() is owner
    assert owner.worker_config()['mode'] == 'session'
    assert len(adapter.calls) == 1
    await owner.aclose()
    await owner.aclose()
    assert adapter.closed == 1
    with pytest.raises(SessionClosedError):
        owner.worker_config()
    with pytest.raises(SessionClosedError):
        await owner()


@pytest.mark.asyncio
async def test_refresh_boundary_failure_and_retry():
    now = [0]
    adapter = Adapter()
    adapter.material = temp(100)
    owner = session(adapter, now)
    await owner()
    now[0] = 69
    await owner()
    assert len(adapter.calls) == 1
    now[0] = 70
    adapter.error = True
    with pytest.raises(ContractError, match='^Bedrock session acquisition failed$'):
        await owner()
    now[0] = 100
    with pytest.raises(ContractError, match='Expired'):
        owner.worker_config()
    adapter.error = False
    adapter.material = temp(200)
    await owner()
    assert adapter.calls[-1][1].expires_at == 100
    await owner.aclose()


@pytest.mark.asyncio
async def test_invalid_adapter_material_sanitized():
    adapter = Adapter()
    adapter.material = object()
    owner = session(adapter)
    with pytest.raises(ContractError, match='^Bedrock session acquisition failed$'):
        await owner()
    await owner.aclose()


@pytest.mark.asyncio
async def test_concurrent_acquisition_coalesces():
    adapter = Adapter()
    adapter.release.clear()
    owner = session(adapter)
    first = asyncio.create_task(owner())
    await adapter.entered.wait()
    second = asyncio.create_task(owner())
    await asyncio.sleep(0)
    adapter.release.set()
    assert await first is owner
    assert await second is owner
    assert len(adapter.calls) == 1
    await owner.aclose()


@pytest.mark.asyncio
async def test_close_drains_pending_and_prevents_publication():
    adapter = Adapter()
    adapter.release.clear()
    owner = session(adapter)
    pending = asyncio.create_task(owner())
    await adapter.entered.wait()
    await owner.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert owner._pending is None
    assert owner._material is None
    assert adapter.closed == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_preserves_shared_acquisition():
    adapter = Adapter()
    adapter.release.clear()
    owner = session(adapter)
    pending = asyncio.create_task(owner())
    await adapter.entered.wait()
    other = asyncio.create_task(owner())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    adapter.release.set()
    assert await other is owner
    assert len(adapter.calls) == 1
    await owner.aclose()


@pytest.mark.asyncio
async def test_failed_close_can_retry():
    adapter = Adapter()
    adapter.close_error = True
    owner = session(adapter)
    with pytest.raises(ContractError, match='^Bedrock session cleanup failed$'):
        await owner.aclose()
    adapter.close_error = False
    await owner.aclose()
    assert adapter.closed == 2


@pytest.mark.asyncio
async def test_concurrent_close_and_cancellation_finish_owned_cleanup():
    adapter = Adapter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        adapter.closed += 1

    adapter.aclose = close
    owner = session(adapter)
    first = asyncio.create_task(owner.aclose())
    await entered.wait()
    second = asyncio.create_task(owner.aclose())
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert adapter.closed == 1


@pytest.mark.asyncio
async def test_cancelled_only_waiter_retains_task_until_close():
    adapter = Adapter()
    adapter.release.clear()
    owner = session(adapter)
    waiter = asyncio.create_task(owner())
    await adapter.entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert owner._pending is not None
    await owner.aclose()
    assert owner._pending is None
    assert owner._material is None


@pytest.mark.asyncio
async def test_adapter_suppressing_cancel_cannot_publish_after_close():
    adapter = Adapter()
    entered = asyncio.Event()

    async def acquire(request, *, previous=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return temp(100)

    adapter.acquire = acquire
    owner = session(adapter)
    waiter = asyncio.create_task(owner())
    await entered.wait()
    await owner.aclose()
    with pytest.raises(SessionClosedError):
        await waiter
    assert owner._material is None


@pytest.mark.asyncio
async def test_interaction_requirement_preserves_type_and_sanitizes_text():
    from api_agent.providers.contracts import InteractionRequiredError
    adapter = Adapter()

    async def acquire(request, *, previous=None):
        raise InteractionRequiredError('secret adapter input')

    adapter.acquire = acquire
    owner = session(adapter)
    with pytest.raises(InteractionRequiredError, match='^Bedrock session acquisition requires interaction[.]$'):
        await owner()
    await owner.aclose()


@pytest.mark.asyncio
async def test_session_preserves_selected_credential_error_identity_and_source():
    from api_agent.providers.contracts import UnattendedCredentialError
    original = UnattendedCredentialError('Selected credentials unavailable.', index_source='dotenv')
    adapter = Adapter()

    async def acquire(request, *, previous=None):
        raise original

    adapter.acquire = acquire
    owner = session(adapter)
    try:
        with pytest.raises(UnattendedCredentialError) as error:
            await owner()
        assert error.value is original and error.value.index_source == 'dotenv'
    finally:
        await owner.aclose()
    assert adapter.closed == 1


@pytest.mark.asyncio
async def test_first_waiter_deadline_does_not_own_shared_acquisition():
    from api_agent.providers.progress import component_timeout
    adapter = Adapter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def acquire(request, *, previous=None):
        adapter.calls.append(request)
        entered.set()
        await release.wait()
        return adapter.material

    async def short_waiter():
        async with component_timeout('first waiter', 0.02):
            await owner()

    adapter.acquire = acquire
    owner = session(adapter)
    first = asyncio.create_task(short_waiter())
    await entered.wait()
    second = asyncio.create_task(owner())
    with pytest.raises(TimeoutError):
        await first
    release.set()
    assert await second is owner
    assert len(adapter.calls) == 1
    assert owner._pending is None
    await owner.aclose()


@pytest.mark.asyncio
async def test_close_caller_deadline_still_drains_independent_cleanup():
    from api_agent.providers.progress import component_timeout
    adapter = Adapter()

    async def close():
        await asyncio.sleep(0.03)
        adapter.closed += 1

    adapter.aclose = close
    owner = session(adapter)
    with pytest.raises(TimeoutError):
        async with component_timeout('closing caller', 0.01):
            await owner.aclose()
    assert adapter.closed == 1
    await owner.aclose()
    assert adapter.closed == 1


@pytest.mark.asyncio
async def test_session_preserves_sanitized_setup_prerequisite():
    from api_agent.providers.contracts import SetupPrerequisiteError

    class MissingPrerequisite(Adapter):
        async def acquire(self, request, *, previous=None):
            raise SetupPrerequisiteError('synthetic-sensitive-adapter-detail')

    adapter = MissingPrerequisite()
    owner = session(adapter)
    try:
        with pytest.raises(SetupPrerequisiteError) as error:
            await owner()
        assert str(error.value) == 'Selected Bedrock session adapter prerequisites are unavailable; check requirements.'
        assert 'synthetic-sensitive-adapter-detail' not in str(error.value)
        assert error.value.__suppress_context__ is True
        with pytest.raises(ContractError, match='not been acquired'):
            owner.worker_config()
    finally:
        await owner.aclose()
    assert adapter.closed == 1
