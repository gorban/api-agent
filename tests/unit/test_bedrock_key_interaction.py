"""Offline private key acquisition contracts."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from api_agent.auth.interaction import Reply
from api_agent.auth.interaction_transport import OwnedInteraction
from api_agent.llm_providers.bedrock.auth import create_session
from api_agent.providers.contracts import ContractError, InteractionRequiredError


class Channel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.output = []
        self.closed = False

    def write(self, text):
        self.output.append(text)

    def read(self, timeout):
        assert timeout == 0.1
        return next(self.replies)

    def close(self):
        self.closed = True


class Interaction:
    def __init__(self, replies):
        self.channel = Channel(replies)
        self.labels = []
        self.cancel_event = threading.Event()

    def open(self, label):
        self.labels.append(label)
        return self.channel


async def acquire(mode, owner, **kwargs):
    return await create_session({'BEDROCK_AUTH_MODE': mode, **kwargs.pop('config', {})},
        interactive=kwargs.pop('interactive', True), user_index=kwargs.pop('user_index', None),
        credentials=kwargs.pop('credentials', None), services={'interaction': owner}, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,inputs,expected,prompts', [
    ('api_key', [' opaque-._+/=\r\n '], {'api_key': ' opaque-._+/=\r\n '}, ['Mantle bearer API key:']),
    ('access_key', [' synthetic-ID ', ' synthetic-secret\r\n '],
     {'access_key': ' synthetic-ID ', 'secret_key': ' synthetic-secret\r\n '},
     ['Access-key ID:', 'Secret access key:']),
])
async def test_attended_keys_are_opaque_and_screen_is_owned(mode, inputs, expected, prompts):
    transport = Interaction([None, *[Reply('input', value) for value in inputs]])
    root = OwnedInteraction(transport)
    async def forbidden(*args):
        pytest.fail('no index must not read a vault')
    session = await acquire(mode, root, credentials=forbidden)
    assert {key: session.worker_config()[key] for key in expected} == expected
    assert transport.labels == ['Bedrock credentials']
    assert transport.channel.output == [prompt + '\n' for prompt in prompts]
    assert transport.channel.closed
    assert not root.cancelled and not root.requests
    assert all(value not in repr(session) for value in inputs)
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['api_key', 'access_key'])
@pytest.mark.parametrize('changes', [{'interactive': False}, {'config': {'NO_STD_IN': True}},
                                  {'user_index': True}, {'user_index': -1}, {'user_index': '0'}])
async def test_forbidden_interaction_never_opens(mode, changes):
    owner = Interaction([])
    with pytest.raises(InteractionRequiredError):
        await acquire(mode, owner, **changes)
    assert owner.labels == []


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [None, SimpleNamespace(cancel_event=threading.Event())])
async def test_missing_owner_fails_without_stdin(owner):
    with pytest.raises(InteractionRequiredError):
        await acquire('api_key', owner)


@pytest.mark.asyncio
async def test_cancelled_owner_fails_before_open():
    owner = Interaction([])
    owner.cancel_event.set()
    with pytest.raises(InteractionRequiredError):
        await acquire('api_key', owner)
    assert owner.labels == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [Reply('cancel'), Reply('retry'), Reply('input', 1), Reply('input', 'x' * 65537)])
async def test_cancel_and_invalid_replies_close_without_diagnostics(reply):
    owner = Interaction([reply])
    with pytest.raises(InteractionRequiredError) as failure:
        await acquire('api_key', owner)
    assert str(failure.value) == 'Bedrock credential interaction is unavailable.'
    assert owner.channel.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,replies', [('api_key', [' ']), ('access_key', ['key', '']),
                                        ('access_key', ['ASIAtemporary', 'secret'])])
async def test_invalid_key_material_still_closes(mode, replies):
    owner = Interaction([Reply('input', value) for value in replies])
    with pytest.raises(ContractError):
        await acquire(mode, owner)
    assert owner.channel.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['api_key', 'access_key'])
async def test_selected_identity_never_falls_back_to_attended(mode):
    owner = Interaction([])
    async def reader(index, kind):
        raise ValueError('private-value')
    with pytest.raises(InteractionRequiredError, match='Unable to read selected'):
        await acquire(mode, owner, user_index=4, credentials=reader)
    assert owner.labels == []


@pytest.mark.asyncio
async def test_cleanup_failure_retains_private_owner_for_retry():
    from api_agent.providers.control import CleanupPendingError
    owner = Interaction([Reply('input', 'synthetic-opaque')])
    original = owner.channel.close
    def fail():
        raise RuntimeError('synthetic-opaque')
    owner.channel.close = fail
    with pytest.raises(CleanupPendingError) as failure:
        await acquire('api_key', owner)
    retained, = failure.value.retained_owners
    assert failure.value.owners == (retained,)
    assert repr(retained) == '<KeyInteraction>'
    assert str(failure.value) == 'cleanup pending'
    owner.channel.close = original
    await failure.value.retry()
    assert owner.channel.closed
    assert retained._borrowed is None


@pytest.mark.asyncio
async def test_closed_channel_fails_before_first_prompt():
    owner = Interaction([])
    owner.channel.closed = True
    with pytest.raises(InteractionRequiredError):
        await acquire('api_key', owner)
    assert owner.channel.output == []


@pytest.mark.asyncio
async def test_owner_cancelled_after_open_fails_before_prompt():
    owner = Interaction([])
    def cancel(label):
        owner.labels.append(label)
        owner.cancel_event.set()
        return owner.channel
    owner.open = cancel
    with pytest.raises(InteractionRequiredError):
        await acquire('api_key', owner)
    assert owner.channel.output == []
    assert owner.channel.closed


@pytest.mark.asyncio
async def test_cancellation_drains_owned_worker_before_return():
    owner = Interaction([])
    entered, release = threading.Event(), threading.Event()
    def wait(timeout):
        entered.set()
        release.wait(2)
        return Reply('input', 'synthetic-opaque')
    owner.channel.read = wait
    task = asyncio.create_task(acquire('api_key', owner))
    while not entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.channel.closed


@pytest.mark.asyncio
async def test_cancel_between_fields_cannot_disclose_first_field():
    owner = Interaction([Reply('input', 'synthetic-first'), Reply('cancel', 'synthetic-second')])
    with pytest.raises(InteractionRequiredError) as failure:
        await acquire('access_key', owner)
    assert owner.channel.output == ['Access-key ID:\n', 'Secret access key:\n']
    assert 'synthetic' not in str(failure.value)
    assert owner.channel.closed
