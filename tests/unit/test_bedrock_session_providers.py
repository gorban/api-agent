"""Offline session adapter loading and ownership tests."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock.session_providers import SDKProfileAcquirer, load_acquirer
from api_agent.providers.contracts import InteractionRequiredError, SessionClosedError
from api_agent.providers.loading import ProviderError


GOOD = '''
class Adapter:
    version = 1
    async def acquire(self, request, *, previous=None):
        return (request, previous)
    async def aclose(self):
        pass

def factory(config, services):
    services['created'] = Adapter()
    return services['created']
'''


@pytest.fixture
def adapter_file(tmp_path):
    def write(source=GOOD):
        path = tmp_path / 'adapter.py'
        path.write_text(source)
        return str(path) + ':factory'
    return write


@pytest.mark.asyncio
async def test_builtin_is_offline_and_closes():
    owner = await load_acquirer('', {}, {})
    assert isinstance(owner, SDKProfileAcquirer)
    assert (await owner.acquire(SimpleNamespace(user_index=None))).kind == 'frozen'
    await owner.aclose()
    await owner.aclose()
    with pytest.raises(SessionClosedError):
        await owner.acquire(SimpleNamespace(user_index=None))


@pytest.mark.asyncio
async def test_builtin_rejects_selected_index():
    owner = await load_acquirer('', {}, {})
    with pytest.raises(InteractionRequiredError):
        await owner.acquire(SimpleNamespace(user_index=0))


@pytest.mark.asyncio
async def test_explicit_factory_owns_relative_imports_until_close(tmp_path, adapter_file):
    (tmp_path / 'helper.py').write_text('VALUE = 42\n')
    source = GOOD.replace('return (request, previous)', 'from .helper import VALUE\n        return (VALUE, previous)')
    adapter_file(source)
    services = {'owner_root': tmp_path}
    owner = await load_acquirer('adapter.py:factory', {}, services)
    assert repr(owner) == '<BedrockSessionAcquirer>'
    assert await owner.acquire(None, previous='old') == (42, 'old')
    name = services['created'].__class__.__module__.rsplit('.', 1)[0]
    assert name + '.helper' in sys.modules
    await owner.aclose()
    assert not any(key == name or key.startswith(name + '.') for key in sys.modules)
    with pytest.raises(SessionClosedError):
        await owner.acquire(None)


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['True', '2', 'None', "'1'"])
async def test_invalid_version_is_disposed(adapter_file, version):
    services = {}
    source = GOOD.replace('version = 1', 'version = ' + version).replace('pass', 'self.closed = True')
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, services)
    assert services['created'].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [
    'def acquire(self, request, *, previous=None):',
    'async def acquire(self):',
    'async def acquire(self, request, *, previous=None, required):',
])
async def test_invalid_acquire_contract_is_disposed(adapter_file, replacement):
    source = GOOD.replace('async def acquire(self, request, *, previous=None):', replacement)
    services = {}
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, services)
    assert services['created'].__class__.__module__ not in sys.modules


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [
    'raise RuntimeError("private failure")',
    'def factory(config, services):\n    raise RuntimeError("private failure")',
    'factory = None',
    'def factory(config, services):\n    return None',
    'def factory(config, services):\n    return object()',
    'async def factory(config, services):\n    return None',
])
async def test_explicit_failure_is_sanitized_without_fallback(adapter_file, source):
    before = set(sys.modules)
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$') as caught:
        await load_acquirer(adapter_file(source), {}, {})
    assert 'private' not in str(caught.value)
    assert not {name for name in set(sys.modules) - before if name.startswith('_api_agent_session_')}


@pytest.mark.asyncio
async def test_bad_location_has_safe_diagnostic():
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(None, {}, None)


@pytest.mark.asyncio
async def test_acquire_errors_are_sanitized(adapter_file):
    source = GOOD.replace('return (request, previous)', 'raise RuntimeError("private failure")')
    owner = await load_acquirer(adapter_file(source), {}, {})
    with pytest.raises(ProviderError, match='^bedrock_session_acquisition_failed$'):
        await owner.acquire(None)
    await owner.aclose()


@pytest.mark.asyncio
async def test_acquire_cancellation_is_preserved(adapter_file):
    source = 'import asyncio\n' + GOOD.replace('return (request, previous)', 'raise asyncio.CancelledError')
    owner = await load_acquirer(adapter_file(source), {}, {})
    with pytest.raises(asyncio.CancelledError):
        await owner.acquire(None)
    await owner.aclose()


@pytest.mark.asyncio
async def test_failed_disposal_retains_namespace_for_retry(adapter_file):
    source = GOOD.replace('version = 1', 'version = 2').replace('pass', '''
        if not getattr(self, 'attempted', False):
            self.attempted = True
            raise RuntimeError('private cleanup failure')''')
    services = {}
    with pytest.raises(ProviderError, match='^bedrock_session_cleanup_pending$') as caught:
        await load_acquirer(adapter_file(source), {}, services)
    module = services['created'].__class__.__module__
    assert module in sys.modules
    await caught.value.retained_owners[0].aclose()
    assert module not in sys.modules


@pytest.mark.asyncio
@pytest.mark.parametrize('declaration', [
    'async def acquire(self, request, previous=None):',
    'async def acquire(self, request, *, previous=1):',
    'async def acquire(self, request, *, previous=None, optional=None):',
    'async def acquire(self, request, **previous):',
])
async def test_previous_must_be_exact_keyword_protocol(adapter_file, declaration):
    source = GOOD.replace('async def acquire(self, request, *, previous=None):', declaration)
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, {})


@pytest.mark.asyncio
async def test_close_cannot_require_or_accept_arguments(adapter_file):
    source = GOOD.replace('async def aclose(self):', 'async def aclose(self, optional=None):')
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, {})


@pytest.mark.asyncio
async def test_async_factory_body_does_not_execute(adapter_file):
    services = {}
    source = 'async def factory(config, services):\n    services["executed"] = True\n'
    with pytest.raises(ProviderError):
        await load_acquirer(adapter_file(source), {}, services)
    assert services == {}


@pytest.mark.asyncio
async def test_scheduled_factory_result_is_cancelled(adapter_file):
    services = {}
    source = GOOD.replace('def factory(config, services):', 'def unused(config, services):') + '''
import asyncio
def factory(config, services):
    services['task'] = asyncio.create_task(asyncio.sleep(100))
    return services['task']
'''
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, services)
    assert services['task'].cancelled()


@pytest.mark.asyncio
async def test_import_cancellation_is_preserved(adapter_file):
    source = 'import asyncio\nraise asyncio.CancelledError\n'
    with pytest.raises(asyncio.CancelledError):
        await load_acquirer(adapter_file(source), {}, {})


@pytest.mark.asyncio
async def test_cancelled_rollback_retains_owner_for_retry(adapter_file):
    source = 'import asyncio\n' + GOOD.replace('version = 1', 'version = 2').replace('pass', '''
        if not getattr(self, 'attempted', False):
            self.attempted = True
            raise asyncio.CancelledError''')
    services = {}
    with pytest.raises(asyncio.CancelledError) as caught:
        await load_acquirer(adapter_file(source), {}, services)
    module = services['created'].__class__.__module__
    assert module in sys.modules
    await caught.value.retained_owners[0].aclose()
    assert module not in sys.modules


@pytest.mark.asyncio
async def test_malformed_synchronous_close_is_disposed(adapter_file):
    source = GOOD.replace('async def aclose(self):\n        pass', 'def aclose(self):\n        self.closed = True')
    services = {}
    with pytest.raises(ProviderError, match='^bedrock_session_initialization_failed$'):
        await load_acquirer(adapter_file(source), {}, services)
    assert services['created'].closed
    assert services['created'].__class__.__module__ not in sys.modules


@pytest.mark.asyncio
async def test_custom_interaction_keeps_safe_classification(adapter_file):
    source = 'from api_agent.providers.contracts import InteractionRequiredError\n' + GOOD.replace(
        'return (request, previous)', 'raise InteractionRequiredError("raw adapter diagnostic")')
    owner = await load_acquirer(adapter_file(source), {}, {})
    with pytest.raises(InteractionRequiredError, match='^Bedrock session acquisition requires interaction[.]$'):
        await owner.acquire(None)
    await owner.aclose()


@pytest.mark.asyncio
async def test_loaded_session_preserves_safe_interaction(adapter_file):
    from api_agent.llm_providers.bedrock.acquisition import SessionRequest
    from api_agent.llm_providers.bedrock.sessions import AcquiredSession

    source = 'from api_agent.providers.contracts import InteractionRequiredError\n' + GOOD.replace(
        'return (request, previous)', 'raise InteractionRequiredError("raw adapter diagnostic")')
    owner = await load_acquirer(adapter_file(source), {}, {})
    session = AcquiredSession(owner, SessionRequest(None, None, None, True))
    with pytest.raises(InteractionRequiredError, match='^Bedrock session acquisition requires interaction[.]$'):
        await session()
    await session.aclose()


DESCRIPTOR_CLOSE = '''
class Adapter:
    version = 2
    broken = True
    async def acquire(self, request, *, previous=None):
        return None
    @property
    def aclose(self):
        if self.broken:
            raise RuntimeError('raw close diagnostic')
        return self.close
    async def close(self):
        self.closed = True

def factory(config, services):
    services['created'] = Adapter()
    return services['created']
'''


@pytest.mark.asyncio
async def test_rejected_close_descriptor_retains_safe_retry_owner(adapter_file):
    services = {}
    with pytest.raises(ProviderError, match='^bedrock_session_cleanup_pending$') as caught:
        await load_acquirer(adapter_file(DESCRIPTOR_CLOSE), {}, services)
    adapter = services['created']
    module = adapter.__class__.__module__
    assert module in sys.modules
    adapter.broken = False
    await caught.value.retained_owners[0].aclose()
    assert adapter.closed
    assert module not in sys.modules


@pytest.mark.asyncio
async def test_valid_close_descriptor_failure_retains_safe_retry_owner(adapter_file):
    services = {}
    source = DESCRIPTOR_CLOSE.replace('version = 2', 'version = 1').replace('broken = True', 'broken = False')
    owner = await load_acquirer(adapter_file(source), {}, services)
    adapter = services['created']
    adapter.broken = True
    module = adapter.__class__.__module__
    with pytest.raises(ProviderError, match='^bedrock_session_cleanup_pending$') as caught:
        await owner.aclose()
    assert module in sys.modules
    assert caught.value.retained_owners == (owner,)
    adapter.broken = False
    await caught.value.retained_owners[0].aclose()
    assert adapter.closed
    assert module not in sys.modules


@pytest.mark.asyncio
async def test_valid_owner_cancelled_close_keeps_retry_owner(adapter_file):
    source = 'import asyncio\n' + GOOD.replace('pass', '''
        if not getattr(self, 'attempted', False):
            self.attempted = True
            raise asyncio.CancelledError''')
    owner = await load_acquirer(adapter_file(source), {}, {})
    with pytest.raises(asyncio.CancelledError) as caught:
        await owner.aclose()
    assert caught.value.retained_owners == (owner,)
    await caught.value.retained_owners[0].aclose()


@pytest.mark.asyncio
async def test_acquirer_preserves_sanitized_setup_prerequisite(adapter_file):
    from api_agent.providers.contracts import SetupPrerequisiteError
    source = GOOD.replace('return (request, previous)',
        "from api_agent.providers.contracts import SetupPrerequisiteError\n        raise SetupPrerequisiteError('synthetic-sensitive-helper-detail')")
    owner = await load_acquirer(adapter_file(source), {}, {})
    try:
        with pytest.raises(SetupPrerequisiteError) as error:
            await owner.acquire(None)
        assert str(error.value) == 'Selected Bedrock session adapter prerequisites are unavailable; check requirements.'
        assert 'synthetic-sensitive-helper-detail' not in str(error.value)
        assert error.value.__suppress_context__ is True
    finally:
        await owner.aclose()


@pytest.fixture(autouse=True)
def frozen_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    async def acquire(self, request, **kwargs):
        return SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret'), {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)
