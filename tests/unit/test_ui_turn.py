"""UI turn evidence completes before the next response begins."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.testing.ui_turn import UITurn, ui_turn
from api_agent.testing.inference_content import content_boundary

CONVERSATION = '11111111-1111-4111-8111-111111111111'
TURN = '22222222-2222-4222-8222-222222222222'


def prepared(capture):
    owner = SimpleNamespace(ui_turn_lock=asyncio.Lock(), emissions=content_boundary({}), capture=capture)
    return SimpleNamespace(services={'ui_turn_evidence':owner})


def test_concurrent_turn_waits_until_capture_finishes():
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def capture(phase, *, owner):
            assert phase == 'success' and owner['turn_id'] == TURN
            order.append('capture')
            entered.set()
            await asyncio.wait_for(release.wait(),1)
            order.append('captured')
        session = prepared(capture)
        async def first():
            async with ui_turn(session) as turn:
                turn.bind(conversation_id=CONVERSATION, turn_id=TURN)
                order.append('response')
        async def second():
            async with ui_turn(session):
                order.append('next')
        task = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(),1)
        next_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert order == ['response','capture']
        release.set()
        await asyncio.gather(task,next_task)
        assert order == ['response','capture','captured','next']
    asyncio.run(exercise())


@pytest.mark.parametrize('error', [AssertionError('response failed'), asyncio.CancelledError()])
def test_failed_turn_captures_and_preserves_primary(error):
    capture = AsyncMock(side_effect=RuntimeError('capture failed'))
    async def exercise():
        async with ui_turn(prepared(capture)) as turn:
            turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
            raise error
    with pytest.raises(type(error)) as caught:
        asyncio.run(exercise())
    assert caught.value is error
    assert capture.call_args.args == ('failure',)


def test_pre_submit_failure_does_not_invent_owner():
    capture = AsyncMock()
    async def exercise():
        async with ui_turn(prepared(capture)):
            raise AssertionError('before submission')
    with pytest.raises(AssertionError): asyncio.run(exercise())
    capture.assert_not_awaited()


@pytest.mark.parametrize('value', [None, 2, '', 'opaque', CONVERSATION.replace('1','A',1), TURN.replace('-', '')])
def test_binding_requires_canonical_explicit_ids(value):
    with pytest.raises((ValueError,TypeError)):
        UITurn().bind(conversation_id=CONVERSATION,turn_id=value)


def test_credential_collision_rejected_before_capture():
    boundary = content_boundary({'API_KEY':TURN})
    turn = UITurn(boundary)
    with pytest.raises(ValueError): turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
    assert turn.owner is None


def test_unconfigured_turn_still_validates_binding():
    async def exercise():
        async with ui_turn(SimpleNamespace(services={})) as turn:
            turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
        assert turn.owner == {'kind':'turn','conversation_id':CONVERSATION,'turn_id':TURN}
        with pytest.raises(ValueError):turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
    asyncio.run(exercise())


def test_capture_cancellation_propagates_without_primary_failure():
    async def exercise():
        async with ui_turn(prepared(AsyncMock(side_effect=asyncio.CancelledError()))) as turn:
            turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
    with pytest.raises(asyncio.CancelledError):asyncio.run(exercise())


def test_successful_response_survives_ordinary_capture_failure():
    async def exercise():
        async with ui_turn(prepared(AsyncMock(side_effect=RuntimeError('unavailable')))) as turn:
            turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
        assert turn.owner['turn_id']==TURN
    asyncio.run(exercise())


def test_bound_identifiers_are_frozen():
    turn=UITurn()
    turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
    turn.owner['turn_id']='replacement'
    assert turn.owner['turn_id']==TURN


def test_cancellation_during_capture_releases_lock_without_detached_work():
    async def exercise():
        entered=asyncio.Event()
        finished=asyncio.Event()
        async def capture(*args,**kwargs):
            entered.set()
            try:await asyncio.Event().wait()
            finally:finished.set()
        session=prepared(capture)
        async def first():
            async with ui_turn(session) as turn:
                turn.bind(conversation_id=CONVERSATION,turn_id=TURN)
        task=asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert finished.is_set()
        assert not session.services['ui_turn_evidence'].ui_turn_lock.locked()
        async with ui_turn(session):pass
    asyncio.run(exercise())
