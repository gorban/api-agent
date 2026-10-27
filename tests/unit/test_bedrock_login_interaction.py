import asyncio
import threading
from types import SimpleNamespace

import pytest

from api_agent.auth.interaction import Reply
from api_agent.auth.interaction_transport import OwnedInteraction
from api_agent.llm_providers.bedrock import login_interaction as module
from api_agent.providers.contracts import InteractionRequiredError, ProviderAccessError
from api_agent.providers.control import CleanupPendingError


class Channel:
    def __init__(self):
        self.output = []
        self.reply = None
        self.closed = False

    def write(self, text):
        self.output.append(text)

    def read(self, timeout):
        assert timeout == 0.1
        return self.reply

    def close(self):
        self.closed = True


class Interaction:
    def __init__(self):
        self.channels = []

    def open(self, label):
        assert label == 'AWS sign-in'
        channel = Channel()
        self.channels.append(channel)
        return channel


def bridge(owner=None, interactive=True):
    return module.LoginInteraction(SimpleNamespace(interaction=owner, interactive=interactive))


@pytest.mark.asyncio
async def test_private_fork_keeps_root_and_sibling_live():
    transport = Interaction()
    root = OwnedInteraction(transport)
    sibling = root.fork()
    other = sibling.open('AWS sign-in')
    login = bridge(root)
    await login.open()
    await login.write('private native prompt')
    assert await login.read() is None
    transport.channels[1].reply = Reply('input', 'privatecode')
    assert await login.read() == 'privatecode'
    assert repr(login) == '<LoginInteraction>'
    await login.aclose()
    await login.aclose()
    assert transport.channels[1].closed
    assert not root.cancelled and not sibling.cancelled and not other.closed
    assert root.requests == {sibling}


@pytest.mark.asyncio
async def test_borrowed_nonfork_owner_only_channel_closed():
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    await login.aclose()
    assert owner.channels[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('owner,interactive', [(None, True), (Interaction(), False)])
async def test_missing_interaction_rejects(owner, interactive):
    login = bridge(owner, interactive)
    with pytest.raises(InteractionRequiredError, match='AWS sign-in interaction is unavailable'):
        await login.open()
    await login.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'eof', 'retry', 'unexpected'])
async def test_noninput_is_fixed_failure(action):
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply(action, 'secret')
    with pytest.raises(InteractionRequiredError, match='^AWS sign-in interaction is unavailable.$'):
        await login.read()
    await login.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [123, 'x' * 65537])
async def test_invalid_bounded_strings(value):
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    with pytest.raises(InteractionRequiredError):
        await login.write(value)
    owner.channels[0].reply = Reply('input', value)
    with pytest.raises(InteractionRequiredError):
        await login.read()
    assert owner.channels[0].output == []
    await login.aclose()


@pytest.mark.asyncio
async def test_lifecycle_guards_and_sanitized_errors():
    owner = Interaction()
    login = bridge(owner)
    with pytest.raises(InteractionRequiredError):
        await login.read()
    await login.open()
    with pytest.raises(InteractionRequiredError):
        await login.open()
    def fail(text):
        raise ValueError('private secret')
    owner.channels[0].write = fail
    with pytest.raises(InteractionRequiredError) as error:
        await login.write('secret')
    assert 'secret' not in str(error.value)
    await login.aclose()
    with pytest.raises(InteractionRequiredError):
        await login.open()
    with pytest.raises(InteractionRequiredError):
        await login.write('secret')


@pytest.mark.asyncio
async def test_cancelled_open_is_retained_until_drain_and_retry(monkeypatch):
    monkeypatch.setattr(module, '_SECONDS', 0.02)
    entered, release = threading.Event(), threading.Event()
    owner = Interaction()
    original = owner.open
    def delayed(label):
        entered.set()
        release.wait(2)
        return original(label)
    owner.open = delayed
    login = bridge(owner)
    opening = asyncio.create_task(login.open())
    while not entered.is_set():
        await asyncio.sleep(0)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    with pytest.raises(InteractionRequiredError):
        await login.open()
    with pytest.raises(CleanupPendingError) as error:
        await login.aclose()
    assert error.value.owners == (login,)
    release.set()
    await asyncio.sleep(0.01)
    await error.value.retry()
    assert owner.channels[0].closed
    assert not login._tasks


@pytest.mark.asyncio
async def test_timeout_keeps_worker_and_close_retry(monkeypatch):
    monkeypatch.setattr(module, '_SECONDS', 0.02)
    entered, release = threading.Event(), threading.Event()
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    def delayed(text):
        entered.set()
        release.wait(2)
    owner.channels[0].write = delayed
    with pytest.raises(InteractionRequiredError):
        await login.write('private')
    assert entered.is_set() and login._tasks
    release.set()
    await login.aclose()
    assert not login._tasks


@pytest.mark.asyncio
async def test_channel_close_failure_retains_retry():
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    original = owner.channels[0].close
    def fail():
        raise ValueError('secret')
    owner.channels[0].close = fail
    with pytest.raises(CleanupPendingError) as error:
        await login.aclose()
    assert str(error.value) == 'cleanup pending'
    owner.channels[0].close = original
    await login.aclose()
    assert owner.channels[0].closed


@pytest.mark.asyncio
async def test_concurrent_close_shares_disposal():
    login = bridge(Interaction())
    await login.open()
    await asyncio.gather(login.aclose(), login.aclose())
    assert login._borrowed is None


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive,no_input', [(True, False), (True, True), (False, False)])
@pytest.mark.parametrize('generic', [True, False])
async def test_auth_selects_borrowed_generic_with_legacy_fallback(monkeypatch, interactive, no_input, generic):
    import time
    from api_agent.llm_providers.bedrock import session_providers
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    from api_agent.llm_providers.bedrock.auth import create_session
    requests = []
    class Adapter:
        async def acquire(self, request, *, previous=None):
            requests.append(request)
            return SessionMaterial('temporary', access_key='synthetic', secret_key='synthetic',
                                   session_token='synthetic', expires_at=time.time() + 120)
        async def aclose(self):
            pass
    async def load(*args):
        return Adapter()
    monkeypatch.setattr(session_providers, 'load_acquirer', load)
    legacy, ordinary = object(), object()
    services = {'bedrock_interaction': legacy}
    if generic:
        services['interaction'] = ordinary
    session = await create_session({'NO_STD_IN': no_input}, interactive=interactive,
                                   user_index=None, credentials=None, services=services)
    expected = ordinary if generic else legacy
    assert requests[0].interaction is (expected if interactive and not no_input else None)
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['\n', '\r', '\r\n', '\n\r\n'])
async def test_native_code_removes_trailing_clipboard_line_endings(suffix):
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply('input', 'c3ludGhldGlj' + suffix)

    assert await login.read() == 'c3ludGhldGlj'
    await login.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('value,expected', [
    ('', ''), ('\r\n', ''),
    (' code ', 'code'), (' code \r\n', 'code'),
    ('first\nsecond\n', 'firstsecond'),
    ('first\rsecond\r\n', 'firstsecond'),
])
async def test_native_code_removes_ascii_whitespace(value, expected):
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply('input', value)

    assert await login.read() == expected
    await login.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['ab-c', 'ab_c', 'ab.c', 'ab\u00a0c', 'ab\u2003c'])
async def test_native_code_rejects_unexpected_characters_without_echo(value):
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply('input', value)
    try:
        with pytest.raises(ProviderAccessError) as failure:
            await login.read()
        assert failure.value.kind == 'invalid_request'
        assert failure.value.code == 'invalid_code'
        assert failure.value.execution == 'not_started'
        assert failure.value.message == 'AWS sign-in code contains unexpected characters. Copy the complete code from the current browser sign-in.'
        assert str(failure.value) == failure.value.message + '\ncode: invalid_code'
        assert value not in repr(vars(failure.value))
    finally:
        await login.aclose()


@pytest.mark.asyncio
async def test_native_code_bounds_original_whitespace_before_normalization():
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply('input', ' ' * 65537)
    try:
        with pytest.raises(InteractionRequiredError):
            await login.read()
    finally:
        await login.aclose()


@pytest.mark.asyncio
async def test_native_code_preserves_standard_base64_punctuation():
    owner = Interaction()
    login = bridge(owner)
    await login.open()
    owner.channels[0].reply = Reply('input', ' \t+/8=\r\n\v\f')
    try:
        assert await login.read() == '+/8='
    finally:
        await login.aclose()
