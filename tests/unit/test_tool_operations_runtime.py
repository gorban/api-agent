"""Nodes reach only the implementation-declared operations they list themselves."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.api.application import Runtime
from api_agent.providers.contracts import ContractError
from api_agent.tooling.runtime import invocation_tools, node_tools
from api_agent.workflows.execution import NodeContext, _node_effect_owner
from tool_operations_support import GRAMMARS, build, compose


@pytest.fixture(params=GRAMMARS)
def grammar(request):
    return request.param


async def started(tmp_path, grammar, **kwargs):
    events = []
    plan = compose(tmp_path, build(tmp_path, grammar, **kwargs))
    runtime = Runtime('demo', tool_plan=plan, services={'events': events})
    await runtime.astart()
    return runtime, plan.definitions[0], events


@pytest.mark.asyncio
async def test_declared_operations_dispatch_to_the_owner(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}) as invocation:
            reader = node_tools('reader')['lookup']
            assert await reader.introspect() == {'operation': 'introspect', 'arguments': {}}
            assert (await reader.query(text='{ a }', limit=2))['arguments'] == {'text': '{ a }', 'limit': 2}
            assert (await node_tools('writer')['store'].mutate(id=7))['operation'] == 'mutate'
            assert invocation.effects == ['writer']
    finally:
        await runtime.aclose()
    assert events == [('introspect', {}), ('query', {'text': '{ a }', 'limit': 2}),
                      ('mutate', {'id': 7}), ('close',)]


@pytest.mark.asyncio
async def test_only_write_operations_mark_the_active_node_as_effectful(tmp_path, grammar):
    runtime, definition, _ = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
            for node, port, operation, effects in (('reader', 'lookup', 'query', []),
                                                   ('writer', 'store', 'mutate', [True])):
                context = NodeContext(node, (), (), {})
                with _node_effect_owner(context):
                    await getattr(node_tools(node)[port], operation)()
                assert context._effects == effects
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_nodes_cannot_reach_undeclared_ports_operations_or_other_nodes(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}) as invocation:
            reader, writer = node_tools('reader'), node_tools('writer')
            assert list(reader) == ['lookup'] and list(writer) == ['store']
            with pytest.raises(ContractError, match='port is not declared'):
                reader['absent']
            with pytest.raises(ContractError, match='port is not declared'):
                reader['store']
            with pytest.raises(ContractError, match='port is not declared'):
                writer['lookup']
            with pytest.raises(ContractError, match='port is not declared'):
                node_tools('unrelated')['lookup']
            # Declared by the implementation, but not listed on this node.
            with pytest.raises(ContractError, match='operation is not declared'):
                reader['lookup'].mutate
            with pytest.raises(ContractError, match='operation is not declared'):
                writer['store'].query
            # Not declared by the implementation at all, including the fixed vocabulary.
            for name in ('explode', 'describe', 'prepare', 'verify', 'execute', 'invoke'):
                with pytest.raises(ContractError, match='operation is not declared'):
                    getattr(reader['lookup'], name)
            assert invocation.effects == []
    finally:
        await runtime.aclose()
    assert events == [('close',)]


@pytest.mark.asyncio
async def test_dispatch_rechecks_the_implementation_even_if_a_node_lists_a_name(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    forged = definition.tool_usages[0].__class__(
        'reader', 'lookup', definition.tool_usages[0].instance, 'node',
        definition.tool_usages[0].declaration, 1, ('introspect', 'explode'))
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}) as invocation:
            with pytest.raises(ContractError, match='not declared by its implementation'):
                await invocation.dispatch(forged, 'explode')
            with pytest.raises(ContractError, match='keyword arguments only'):
                await invocation.dispatch(forged, 'introspect', {'positional': True})
            with pytest.raises(ContractError, match='keyword arguments only'):
                await node_tools('reader')['lookup'].introspect({'positional': True})
    finally:
        await runtime.aclose()
    assert events == [('close',)]


@pytest.mark.asyncio
async def test_argument_names_may_shadow_dispatch_parameters(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
            await node_tools('reader')['lookup'].query(use='a', operation='b')
    finally:
        await runtime.aclose()
    assert events[0] == ('query', {'use': 'a', 'operation': 'b'})


@pytest.mark.asyncio
async def test_view_is_closed_with_the_invocation(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}):
            retained = node_tools('reader')['lookup']
            await retained.introspect()
        with pytest.raises(ContractError, match='closed'):
            await retained.introspect()
    finally:
        await runtime.aclose()
    assert [event[0] for event in events] == ['introspect', 'close']


@pytest.mark.asyncio
async def test_the_earlier_tool_resolver_cannot_reach_implementation_declared_operations(tmp_path, grammar):
    runtime, definition, events = await started(tmp_path, grammar)
    try:
        async with invocation_tools(definition, {'tools': runtime.tools_for(definition)}) as invocation:
            context = NodeContext('writer', (), (), {})
            with pytest.raises(ContractError, match=r'context\.tools\[port\]'):
                context.tool('store')
            assert invocation.effects == []
    finally:
        await runtime.aclose()
    assert ('mutate', {'id': 7}) not in events


def declared(node, port):
    from pathlib import Path
    from api_agent.workflows.definition import ToolUsage
    return SimpleNamespace(definition_id='d', tool_usages=(
        ToolUsage(node, port, Path('/tool.toml'), 'node', Path('/flow.md'), 1, ('invoke',)),))


def test_the_earlier_tool_resolver_does_not_expose_the_raw_borrowed_tool():
    borrowed = type('Borrowed', (), {'description': 'kept', 'call': None, 'view': None, 'owner': None})()
    definition = declared('read', 'primary')
    resolver = SimpleNamespace(definition=definition,
                               for_node=lambda node: SimpleNamespace(borrow=lambda port: borrowed,
                                                                     instance=lambda port: port))

    async def scenario():
        async with invocation_tools(definition, {'tools': resolver}):
            tool = NodeContext('read', (), (), {}).tool('primary')
            assert tool.description == 'kept'
            for name in ('call', 'view', 'owner'):
                with pytest.raises(ContractError, match='not available'):
                    getattr(tool, name)
    asyncio.run(scenario())


def test_the_earlier_tool_resolver_still_reaches_fixed_operation_tools_of_a_runtime():
    from api_agent.tooling.runtime import NodeResolver
    calls = []
    async def invoke(prepared):
        calls.append(prepared)
        return 'done'
    tools = (SimpleNamespace(id='sample/other', interface='operations@1'),
             SimpleNamespace(id='sample/tool', interface='tool@1'))
    definition = SimpleNamespace(definition_id='d', graph_id='g', layout=SimpleNamespace(
        steps=(SimpleNamespace(id='read', kind='node', parent=None),)), tool_usages=declared('read', 'primary').tool_usages)
    runtime = SimpleNamespace(tool_plan=SimpleNamespace(tools=tools, bindings={('d', 'node', 'read', 'primary'): 'sample/tool'}),
                              borrow_tool=lambda instance: SimpleNamespace(invoke=invoke))
    resolver = SimpleNamespace(definition=definition,
                               for_node=lambda node: NodeResolver(runtime, definition, node))
    contexts = []

    async def scenario():
        async with invocation_tools(definition, {'tools': resolver}):
            contexts.append(NodeContext('read', (), (), {}))
            return await contexts[0].tool('primary').invoke('request')
    assert asyncio.run(scenario()) == 'done'
    assert calls == ['request'] and contexts[0]._effects == [True]
