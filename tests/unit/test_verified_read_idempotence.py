"""verified-read@1 is read-only by contract, so its fixed operations are idempotent."""
import asyncio

import pytest

from api_agent.tooling import operations, runtime
from api_agent.tooling.runtime import OwnerTools, invocation_tools, node_tools
from api_agent.workflows.execution import NodeContext, _node_effect_owner
from api_agent.workflows.tool_gate import ToolGate
from test_node_tools import definition
from test_tool_gate import Owner

VERDICT = {'relevant': True, 'grounded': True}


def test_the_fixed_vocabulary_is_declared_idempotent():
    assert operations.FIXED_IDEMPOTENT == frozenset(operations.FIXED_OPERATIONS)


@pytest.fixture(params=[True, False], ids=['idempotent', 'vocabulary-without-idempotence'])
def idempotent(request, monkeypatch):
    if not request.param:
        monkeypatch.setattr(operations, 'FIXED_IDEMPOTENT', frozenset())
        monkeypatch.setattr(runtime, 'FIXED_IDEMPOTENT', frozenset())
    return request.param


@pytest.mark.asyncio
async def test_a_gated_execution_marks_an_effect_only_when_execute_is_not_idempotent(idempotent):
    gate = ToolGate(Owner('synthetic'), invocation_id='call')
    verified = await gate.verify(await gate.prepare({'query': 'one'}), relevance=lambda _: VERDICT)
    context = NodeContext('run', (), (), {})

    with _node_effect_owner(context):
        await gate.execute(verified)

    assert context._effects == ([] if idempotent else [True])
    assert context._dispatched() is not idempotent


@pytest.mark.asyncio
async def test_a_node_execution_records_an_invocation_effect_only_when_execute_is_not_idempotent(idempotent):
    selected = definition()
    owner = Owner('synthetic')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}) as invocation:
        prepared = await node_tools('plan')['reader'].prepare({'query': 'one'})
        grant = await node_tools('review')['check'].verify(prepared, relevance=lambda _: VERDICT)
        await node_tools('run')['primary'].execute(grant)
        context = NodeContext('run', (), (), {})
        assert invocation.effects == ([] if idempotent else ['run'])
        assert context._dispatched() is not idempotent


def test_a_raw_tool_view_marks_an_effect_only_when_execute_is_not_idempotent(idempotent):
    from types import SimpleNamespace
    from test_tool_operations_runtime import declared
    calls = []
    async def invoke(prepared):
        calls.append(prepared)
        return 'done'
    selected = declared('read', 'primary')
    resolver = SimpleNamespace(definition=selected, for_node=lambda node: SimpleNamespace(
        borrow=lambda port: SimpleNamespace(invoke=invoke), instance=lambda port: port))
    contexts = []

    async def scenario():
        async with invocation_tools(selected, {'tools': resolver}):
            contexts.append(NodeContext('read', (), (), {}))
            return await contexts[0].tool('primary').invoke('request')

    assert asyncio.run(scenario()) == 'done'
    assert contexts[0]._effects == ([] if idempotent else [True])
