"""Selected reader ownership uses synthetic in-memory values only."""
import asyncio
import pytest
from api_agent.llm_providers.bedrock.auth import SelectedCredentialReader, create_session
from api_agent.providers.contracts import InteractionRequiredError, SessionClosedError, ContractError


@pytest.mark.asyncio
async def test_closed_reader_cannot_start_new_read():
    calls = []
    async def read(index, kind):
        calls.append((index, kind))
        return 'synthetic'
    owner = SelectedCredentialReader(read, 2)
    assert repr(owner) == '<SelectedCredentialReader>'
    assert await owner(2, 'password') == 'synthetic'
    await owner.aclose()
    await owner.aclose()
    with pytest.raises(SessionClosedError):
        await owner(2, 'password')
    assert calls == [(2, 'password')]


@pytest.mark.asyncio
@pytest.mark.parametrize('index,selected,kind', [(True,1,'password'), (1,True,'password'), (-1,-1,'password'), (2,1,'password'), (1,1,[]), (1,1,'invalid')])
async def test_reader_rejects_invalid_selection(index, selected, kind):
    owner = SelectedCredentialReader(None, selected)
    with pytest.raises(InteractionRequiredError, match='selected account'):
        await owner(index, kind)
    await owner.aclose()


@pytest.mark.asyncio
async def test_reader_error_is_sanitized():
    async def read(index, kind):
        raise RuntimeError('synthetic secret')
    owner = SelectedCredentialReader(read, 1)
    with pytest.raises(InteractionRequiredError, match='^Unable to read selected Bedrock credentials[.]$'):
        await owner(1, 'password')
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_waiter', [False, True])
async def test_inflight_read_is_drained_without_late_publication(cancel_waiter):
    entered = asyncio.Event()
    drained = asyncio.Event()
    async def read(index, kind):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            drained.set()
            return 'late synthetic'
    owner = SelectedCredentialReader(read, 1)
    waiter = asyncio.create_task(owner(1, 'password'))
    await entered.wait()
    if cancel_waiter:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    else:
        await owner.aclose()
        assert drained.is_set()
        with pytest.raises(SessionClosedError):
            await waiter
    assert drained.is_set()
    assert not owner._reads
    await owner.aclose()


@pytest.mark.asyncio
async def test_cancelled_concurrent_close_drains_reader():
    entered = asyncio.Event()
    release = asyncio.Event()
    async def read(index, kind):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return 'late'
    owner = SelectedCredentialReader(read, 1)
    waiter = asyncio.create_task(owner(1, 'password'))
    await entered.wait()
    first = asyncio.create_task(owner.aclose())
    await asyncio.sleep(0)
    second = asyncio.create_task(owner.aclose())
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    with pytest.raises((SessionClosedError, asyncio.CancelledError)):
        await waiter
    assert not owner._reads


@pytest.mark.asyncio
@pytest.mark.parametrize('setup_fails', [False, True])
async def test_saved_adapter_request_reader_revoked_on_close_and_failed_setup(monkeypatch, setup_fails):
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    from api_agent.llm_providers.bedrock import session_providers
    requests = []
    calls = []
    class Adapter:
        async def acquire(self, request, *, previous=None):
            requests.append(request)
            if setup_fails:
                raise RuntimeError('synthetic adapter failure')
            return SessionMaterial('temporary', user_index=2, access_key='synthetic', secret_key='synthetic', session_token='synthetic', expires_at=10**12)
        async def aclose(self):
            pass
    async def load(*args):
        return Adapter()
    async def read(index, kind):
        calls.append((index, kind))
        return 'synthetic'
    monkeypatch.setattr(session_providers, 'load_acquirer', load)
    if setup_fails:
        with pytest.raises(ContractError):
            await create_session({}, interactive=False, user_index=2, credentials=read)
    else:
        session = await create_session({}, interactive=False, user_index=2, credentials=read)
        await session.aclose()
    with pytest.raises(SessionClosedError):
        await requests[0].credentials(2, 'password')
    assert calls == []


@pytest.mark.asyncio
async def test_actual_custom_provider_revokes_retained_request(tmp_path):
    from api_agent.llm_providers.bedrock.factory import factory
    path = tmp_path / 'reader_adapter.py'
    path.write_text('''
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
class Adapter:
    version = 1
    def __init__(self, services): self.services = services
    async def acquire(self, request, *, previous=None):
        self.services['requests'].append(request)
        return SessionMaterial('temporary', user_index=2, access_key='synthetic', secret_key='synthetic', session_token='synthetic', expires_at=10**12)
    async def aclose(self): pass
def factory(config, services): return Adapter(services)
''')
    calls = []
    requests = []
    async def read(index, kind):
        calls.append((index, kind))
        return 'synthetic'
    provider = factory({'BEDROCK_SESSION_PROVIDER': 'reader_adapter.py:factory'}, {'owner_root': tmp_path, 'requests': requests})
    await provider.asetup(interactive=False, user_index=2, credentials=read)
    await provider.aclose()
    with pytest.raises(SessionClosedError):
        await requests[0].credentials(2, 'password')
    assert not calls


@pytest.mark.asyncio
async def test_independent_cleanup_owners_all_attempted_and_retry_safe():
    from api_agent.llm_providers.bedrock.sessions import AcquiredSession
    from api_agent.llm_providers.bedrock.acquisition import SessionRequest
    events = []
    class Reader:
        fail = True
        def revoke(self): events.append('revoke')
        async def aclose(self):
            events.append('reader')
            if self.fail:
                raise RuntimeError('synthetic cleanup secret')
    class Adapter:
        async def aclose(self): events.append('adapter')
    reader = Reader()
    owner = AcquiredSession(Adapter(), SessionRequest(None, None, None, False), credential_owner=reader)
    with pytest.raises(ContractError, match='^Bedrock session cleanup failed$'):
        await owner.aclose()
    assert events == ['revoke', 'reader', 'adapter']
    reader.fail = False
    await owner.aclose()
    assert events == ['revoke', 'reader', 'adapter', 'revoke', 'reader']


@pytest.mark.asyncio
@pytest.mark.parametrize('has_reader,close_fails', [(False,False), (True,False), (True,True)])
async def test_failed_loader_revokes_reader_and_retains_failed_cleanup(monkeypatch, has_reader, close_fails):
    from api_agent.llm_providers.bedrock import auth, session_providers
    readers = []
    original = auth.SelectedCredentialReader
    class Reader(original):
        def __init__(self, *args):
            super().__init__(*args)
            readers.append(self)
        async def aclose(self):
            await super().aclose()
            if close_fails:
                raise ContractError('synthetic cleanup failure')
    async def load(*args):
        raise ContractError('synthetic load failure')
    async def read(*args):
        return 'synthetic'
    monkeypatch.setattr(auth, 'SelectedCredentialReader', Reader)
    monkeypatch.setattr(session_providers, 'load_acquirer', load)
    with pytest.raises(ContractError) as caught:
        await create_session({}, interactive=False, user_index=2, credentials=read if has_reader else None)
    if has_reader:
        with pytest.raises(SessionClosedError):
            await readers[0](2, 'password')
    if close_fails:
        assert caught.value.retained_owners == (readers[0],)


@pytest.mark.asyncio
async def test_failed_setup_retains_retry_owner_with_reader_revoked(monkeypatch):
    from api_agent.llm_providers.bedrock import session_providers
    requests = []
    class Adapter:
        fail = True
        async def acquire(self, request, *, previous=None):
            requests.append(request)
            raise RuntimeError('synthetic acquire failure')
        async def aclose(self):
            if self.fail:
                raise RuntimeError('synthetic cleanup failure')
    adapter = Adapter()
    async def load(*args): return adapter
    async def read(*args): raise AssertionError('revoked reader started')
    monkeypatch.setattr(session_providers, 'load_acquirer', load)
    with pytest.raises(ContractError, match='cleanup failed') as caught:
        await create_session({}, interactive=False, user_index=2, credentials=read)
    with pytest.raises(SessionClosedError):
        await requests[0].credentials(2, 'password')
    adapter.fail = False
    await caught.value.retained_owners[0].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('waiter_first', [True, False])
async def test_close_does_not_recancel_reader_helper_during_awaited_finally(waiter_first):
    entered, cleanup_entered, release, finished = [asyncio.Event() for _ in range(4)]
    async def read(index, kind):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await release.wait()
            finished.set()
    reader = SelectedCredentialReader(read, 1)
    waiter = asyncio.create_task(reader(1, 'password'))
    await entered.wait()
    if waiter_first:
        waiter.cancel()
        await cleanup_entered.wait()
        closing = asyncio.create_task(reader.aclose())
    else:
        closing = asyncio.create_task(reader.aclose())
        await cleanup_entered.wait()
        waiter.cancel()
    try:
        await asyncio.sleep(0.01)
        assert not closing.done()
        assert not finished.is_set()
    finally:
        release.set()
        await closing
        await asyncio.gather(waiter, return_exceptions=True)
    assert finished.is_set()
    assert not reader._reads


@pytest.mark.asyncio
async def test_failed_load_and_reader_cleanup_preserve_all_owners_by_identity(monkeypatch):
    from api_agent.llm_providers.bedrock import auth, session_providers
    from api_agent.providers.loading import ProviderError
    adapter_owner = object()
    distinct_owner = object()
    readers = []
    original = auth.SelectedCredentialReader
    class Reader(original):
        def __init__(self, *args):
            super().__init__(*args)
            readers.append(self)
        async def aclose(self):
            await super().aclose()
            error = RuntimeError('synthetic sensitive cleanup text')
            error.retained_owners = (adapter_owner, self)
            raise error
    async def load(*args):
        raise ProviderError('synthetic loader cleanup pending', retained_owners=(adapter_owner, distinct_owner, adapter_owner))
    async def read(*args): return 'synthetic'
    monkeypatch.setattr(auth, 'SelectedCredentialReader', Reader)
    monkeypatch.setattr(session_providers, 'load_acquirer', load)
    with pytest.raises(ContractError, match='^Bedrock session cleanup failed$') as caught:
        await create_session({}, interactive=False, user_index=1, credentials=read)
    assert caught.value.retained_owners == (adapter_owner, distinct_owner, readers[0])
    with pytest.raises(SessionClosedError):
        await readers[0](1, 'password')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_waiter', [True, False])
async def test_read_timer_cannot_interrupt_helper_finally(monkeypatch, cancel_waiter):
    from api_agent.llm_providers.bedrock import auth
    from api_agent.providers.progress import component_timeout
    entered, cleanup_entered, finished = [asyncio.Event() for _ in range(3)]
    def short_read(name, timeout):
        return component_timeout(name, 0.02 if name == 'Bedrock selected credential read' else timeout)
    monkeypatch.setattr(auth, 'component_timeout', short_read)
    async def read(index, kind):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await asyncio.sleep(0.03)
            finished.set()
    reader = SelectedCredentialReader(read, 1)
    waiter = asyncio.create_task(reader(1, 'password'))
    await entered.wait()
    if cancel_waiter:
        waiter.cancel()
    await cleanup_entered.wait()
    closing = asyncio.create_task(reader.aclose())
    with pytest.raises(asyncio.CancelledError if cancel_waiter else TimeoutError):
        await waiter
    await closing
    assert finished.is_set()
    assert not reader._reads


@pytest.mark.asyncio
async def test_cleanup_deadline_retains_live_helper_for_retry(monkeypatch):
    from api_agent.llm_providers.bedrock import auth
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.progress import report_timeout
    entered, cleanup_entered, release, finished = [asyncio.Event() for _ in range(4)]
    def short_cleanup(name, timeout):
        return report_timeout(name, 0.01 if name == 'Bedrock selected credential cleanup' else timeout)
    monkeypatch.setattr(auth, 'report_timeout', short_cleanup)
    async def read(index, kind):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await release.wait()
            finished.set()
    reader = SelectedCredentialReader(read, 1)
    waiter = asyncio.create_task(reader(1, 'password'))
    await entered.wait()
    closing = asyncio.create_task(reader.aclose())
    await cleanup_entered.wait()
    try:
        with pytest.raises(CleanupPendingError) as caught:
            await closing
        assert caught.value.owners == (reader,)
        assert reader._reads
        assert not finished.is_set()
        with pytest.raises(SessionClosedError):
            await reader(1, 'password')
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await reader.aclose()
    assert finished.is_set()
    assert not reader._reads


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['username', 'password'])
async def test_refresh_reader_preserves_selected_credential_failure(kind):
    from api_agent.providers.contracts import UnattendedCredentialError
    failure = UnattendedCredentialError('Credential retrieval unavailable.', index_source='command_line')
    calls = []
    async def retrieve(index, requested_kind):
        calls.append((index, requested_kind))
        raise failure
    reader = SelectedCredentialReader(retrieve, 6)

    try:
        with pytest.raises(UnattendedCredentialError) as caught:
            await reader(6, kind)
        assert caught.value is failure
        assert caught.value.index_source == 'command_line'
        assert calls == [(6, kind)]
    finally:
        await reader.aclose()
