"""Declared node capabilities share grants only within one graph invocation."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.tooling.runtime import OwnerTools, invocation_tools, node_tools
from api_agent.workflows.definition import ToolUsage, _table, _tool_operations
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.tool_gate import ToolGateError
from test_tool_gate import Owner


def definition():
    return SimpleNamespace(tool_usages=tuple(
        ToolUsage(node, port, Path('/tool.toml'), 'node', Path('/flow.md'), 1, operations)
        for node, port, operations in (
            ('plan', 'reader', ('describe', 'prepare')),
            ('review', 'check', ('verify',)),
            ('run', 'primary', ('execute',)))))


@pytest.mark.asyncio
async def test_cross_node_grants_and_capability_limits():
    selected = definition()
    owner = Owner('synthetic')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}):
        planner = node_tools('plan')['reader']
        with pytest.raises(ContractError, match='operation'):
            await planner.execute(None)
        with pytest.raises(ContractError, match='port'):
            node_tools('run')['reader']
        with pytest.raises(ContractError, match='port'):
            node_tools('unrelated')['primary']
        prepared = await planner.prepare({'query': 'one'})
        grant = await node_tools('review')['check'].verify(prepared,
            relevance=lambda _: {'relevant': True, 'grounded': True})
        runner = node_tools('run')['primary']
        assert (await runner.execute(grant)).data == {'source': 'synthetic'}
        with pytest.raises(ToolGateError, match='stale'):
            await runner.execute(grant)
    assert owner.calls == ['describe', 'validate', 'invoke']
    with pytest.raises(ContractError, match='closed'):
        await planner.describe()


@pytest.mark.asyncio
async def test_concurrent_invocations_reject_each_others_grants():
    selected = definition()
    owner = Owner('synthetic')
    grants, ready = [], asyncio.Event()
    async def turn():
        async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}):
            prepared = await node_tools('plan')['reader'].prepare({'query': 'same'})
            grant = await node_tools('review')['check'].verify(prepared,
                relevance=lambda _: {'relevant': True, 'grounded': True})
            grants.append(grant)
            if len(grants) == 2:
                ready.set()
            await ready.wait()
            other = next(item for item in grants if item is not grant)
            with pytest.raises(ToolGateError, match='missing'):
                await node_tools('run')['primary'].execute(other)
            await node_tools('run')['primary'].execute(grant)
    await asyncio.gather(turn(), turn())
    assert owner.calls.count('invoke') == 2
    async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}):
        with pytest.raises(ToolGateError, match='missing'):
            await node_tools('run')['primary'].execute(grants[0])


@pytest.mark.parametrize('value', ['', 'prepare, prepare', 'invoke', 'prepare verify'])
def test_invalid_explicit_operations_rejected(value):
    with pytest.raises(LayoutError, match='operations'):
        _tool_operations(Path('flow.md'), {'Operations': value, 'line': 1}, 'node')


@pytest.mark.parametrize('scope', ['graph', 'component'])
def test_availability_does_not_grant_operations(scope):
    with pytest.raises(LayoutError, match='operations'):
        _tool_operations(Path('flow.md'), {'Operations': 'describe', 'line': 1}, scope)


def test_optional_tools_column_retains_legacy_syntax():
    body = [(1, '| Port | Instance |'), (2, '| --- | --- |'), (3, '| primary | [tool](tool.toml) |')]
    legacy = _table(Path('flow.md'), body, ('Port', 'Instance'))[0]
    assert _tool_operations(Path('flow.md'), legacy, 'node') == ()
    body = [(1, '| Port | Instance | Operations |'), (2, '| --- | --- | --- |'),
            (3, '| primary | [tool](tool.toml) | describe, prepare |')]
    row = _table(Path('flow.md'), body, ('Port', 'Instance'))[0]
    assert _tool_operations(Path('flow.md'), row, 'node') == ('describe', 'prepare')


@pytest.mark.asyncio
async def test_retained_node_context_never_rebinds_to_new_invocation():
    from api_agent.workflows.execution import NodeContext
    from api_agent.workflows.stateful import StateNodeContext
    selected = definition()
    owner = Owner('synthetic')
    resolver = OwnerTools(selected, owner)
    async with invocation_tools(selected, {'tools': resolver}):
        previous = NodeContext('plan', (), (), {})
        previous_state = StateNodeContext('plan', {}, None, {})
    async with invocation_tools(selected, {'tools': resolver}):
        for context in (previous, previous_state):
            with pytest.raises(ContractError, match='closed'):
                await context.tools['reader'].describe()
    assert owner.calls == []


@pytest.mark.asyncio
async def test_other_definitions_resolver_rejected_before_borrow():
    selected = definition()
    resolver = OwnerTools(definition(), Owner('other'))
    with pytest.raises(ContractError, match='match workflow definition'):
        async with invocation_tools(selected, {'tools': resolver}):
            pytest.fail('mismatched resolver admitted')
    assert resolver.owner.calls == []


@pytest.mark.asyncio
async def test_child_cleanup_failure_retains_retry_without_disposing_parent():
    from api_agent.providers.control import CleanupPendingError
    class Child(Owner):
        blocked = True
        async def aclose(self):
            self.calls.append('close')
            if self.blocked:
                raise RuntimeError('retry disposal')
    child = Child('synthetic')
    class Parent(Owner):
        def for_invocation(self):
            return child
    parent = Parent('parent')
    selected = definition()
    with pytest.raises(CleanupPendingError) as pending:
        async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
            retained = node_tools('plan')['reader']
            await retained.describe()
    assert parent.calls == []
    assert child.calls == ['describe', 'close']
    with pytest.raises(ContractError, match='closed'):
        await retained.describe()
    child.blocked = False
    await pending.value.retry()
    assert child.calls == ['describe', 'close', 'close']


@pytest.mark.asyncio
async def test_cancellation_closes_borrowed_child_and_revokes_capability():
    child = Owner('synthetic')
    class Parent(Owner):
        def for_invocation(self):
            return child
    parent = Parent('parent')
    selected = definition()
    ready = asyncio.Event()
    retained = []
    async def run():
        async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
            retained.append(node_tools('plan')['reader'])
            await retained[0].describe()
            ready.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.calls == ['describe', 'close']
    assert parent.calls == []
    with pytest.raises(ContractError, match='closed'):
        await retained[0].describe()


@pytest.mark.asyncio
async def test_cancellation_drains_active_borrowed_operation_before_close():
    ready = asyncio.Event()
    class Child(Owner):
        async def describe(self):
            self.calls.append('started')
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.calls.append('cancelled')
    child = Child('synthetic')
    class Parent(Owner):
        def for_invocation(self):
            return child
    selected = definition()
    parent = Parent('parent')
    async def run():
        async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
            await node_tools('plan')['reader'].describe()
    task = asyncio.create_task(run())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.calls == ['started', 'cancelled', 'close']
    assert parent.calls == []


@pytest.mark.asyncio
async def test_multiple_instances_keep_grants_separate_and_close_each_view():
    from api_agent.workflows.tool_gate import ToolGate
    first, second = Owner('first'), Owner('second')
    selected = SimpleNamespace(tool_usages=(
        ToolUsage('plan', 'reader', Path('/first.toml'), 'node', Path('/flow.md'), 1, ('prepare', 'verify', 'execute')),
        ToolUsage('run', 'runner', Path('/second.toml'), 'node', Path('/flow.md'), 1, ('execute',))))
    class Resolver:
        definition = selected
        def instance(self, *, port, **scope):
            return port
        def gate(self, *, port, invocation_id, **scope):
            return ToolGate(first if port == 'reader' else second, invocation_id=invocation_id)
    async with invocation_tools(selected, {'tools': Resolver()}):
        assert list(node_tools('plan')) == ['reader']
        assert len(node_tools('run')) == 1
        prepared = await node_tools('plan')['reader'].prepare({'query': 'same'})
        grant = await node_tools('plan')['reader'].verify(prepared,
            relevance=lambda _: {'relevant': True, 'grounded': True})
        with pytest.raises(ToolGateError, match='missing'):
            await node_tools('run')['runner'].execute(grant)
        await node_tools('plan')['reader'].execute(grant)
    assert first.calls == ['describe', 'validate', 'invoke', 'close']
    assert second.calls == ['close']


@pytest.mark.asyncio
async def test_declared_consumer_without_admitted_resolver_fails_before_effects():
    async with invocation_tools(definition(), {}):
        with pytest.raises(ContractError, match='admitted tool bindings'):
            await node_tools('plan')['reader'].describe()


def test_single_owner_compatibility_rejects_ambiguous_instances():
    with pytest.raises(ContractError, match='exactly one'):
        OwnerTools(SimpleNamespace(tool_usages=()), Owner('unused'))


@pytest.mark.asyncio
async def test_async_invocation_factory_rejected_without_creating_child():
    class Parent(Owner):
        async def for_invocation(self):
            pytest.fail('async factory executed')
    selected = definition()
    parent = Parent('parent')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
        with pytest.raises(ContractError, match='must be synchronous'):
            await node_tools('plan')['reader'].describe()
    assert parent.calls == []


@pytest.mark.asyncio
async def test_malformed_child_rejected_before_dispatch_and_closed():
    class InvalidChild:
        calls = []
        def describe(self):
            self.calls.append('effect')
        async def aclose(self):
            self.calls.append('close')
    child = InvalidChild()
    class Parent(Owner):
        def for_invocation(self):
            return child
    selected = definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, Parent('parent'))}):
        with pytest.raises(ContractError, match='async tool methods'):
            await node_tools('plan')['reader'].describe()
    assert child.calls == ['close']


@pytest.mark.asyncio
async def test_coroutine_returned_by_sync_factory_closed_without_running():
    async def accidental_child():
        pytest.fail('rejected coroutine executed')
    coroutine = accidental_child()
    class Parent(Owner):
        def for_invocation(self):
            return coroutine
    selected = definition()
    async with invocation_tools(selected, {'tools': OwnerTools(selected, Parent('parent'))}):
        with pytest.raises(ContractError, match='async tool methods'):
            await node_tools('plan')['reader'].describe()
    assert coroutine.cr_frame is None


@pytest.mark.asyncio
async def test_identical_pending_calls_transfer_out_of_order_and_new_calls_remain_fresh():
    selected = definition()
    owner = Owner('synthetic')
    class Parent(Owner):
        borrowed = 0
        def for_invocation(self):
            self.borrowed += 1
            return owner
    parent = Parent('parent')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
        planner, reviewer, runner = (node_tools(node)[port] for node, port in
            [('plan', 'reader'), ('review', 'check'), ('run', 'primary')])
        first = await planner.prepare({'query': 'same'})
        second = await planner.prepare({'query': 'same'})
        assert first.digest != second.digest
        relevance = lambda _: {'relevant': True, 'grounded': True}
        second_grant = await reviewer.verify(second, relevance=relevance)
        first_grant = await reviewer.verify(first, relevance=relevance)
        await runner.execute(second_grant)
        await runner.execute(first_grant)
        for grant in (first_grant, second_grant):
            with pytest.raises(ToolGateError, match='stale'):
                await runner.execute(grant)
        third = await planner.prepare({'query': 'same'})
        await runner.execute(await reviewer.verify(third, relevance=relevance))
    assert owner.calls == ['describe', 'validate', 'validate', 'invoke', 'invoke', 'validate', 'invoke', 'close']
    assert parent.borrowed == 1
    assert parent.calls == []


@pytest.mark.asyncio
async def test_equal_captured_definition_ids_admit_distinct_snapshot_objects():
    selected = definition()
    selected.definition_id = 'captured'
    admitted = SimpleNamespace(tool_usages=selected.tool_usages, definition_id='captured')
    resolver = OwnerTools(admitted, Owner('synthetic'))
    async with invocation_tools(selected, {'tools': resolver}):
        assert await node_tools('plan')['reader'].describe() is resolver.owner.spec


def test_node_tools_rejects_access_without_invocation():
    from api_agent.workflows.stateful import StateNodeContext
    with pytest.raises(ContractError, match='active workflow invocation'):
        node_tools('plan')
    with pytest.raises(ValueError, match='active workflow invocation'):
        _ = StateNodeContext('plan', {}, None, {}).tools


@pytest.mark.asyncio
async def test_unclosable_malformed_child_rejected_without_attempting_disposal():
    class Parent(Owner):
        def for_invocation(self):
            return object()
    selected = definition()
    parent = Parent('parent')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, parent)}):
        with pytest.raises(ContractError, match='async tool methods'):
            await node_tools('plan')['reader'].describe()
    assert parent.calls == []


@pytest.mark.asyncio
async def test_rejected_scheduled_factory_result_is_cancelled_and_disposed():
    selected = definition()
    future = asyncio.get_running_loop().create_future()
    class Parent(Owner):
        def for_invocation(self):
            return future
    async with invocation_tools(selected, {'tools': OwnerTools(selected, Parent('parent'))}):
        with pytest.raises(ContractError, match='async tool methods'):
            await node_tools('plan')['reader'].describe()
    assert future.cancelled()


@pytest.mark.asyncio
async def test_routing_rejects_forged_grants_and_malformed_candidates_before_dispatch():
    from dataclasses import replace
    from api_agent.workflows.tool_gate import VerifiedCall
    selected = definition()
    owner = Owner('synthetic')
    async with invocation_tools(selected, {'tools': OwnerTools(selected, owner)}):
        planner, reviewer, runner = (node_tools(node)[port] for node, port in
            [('plan', 'reader'), ('review', 'check'), ('run', 'primary')])
        first = await planner.prepare({'query': 'first'})
        second = await planner.prepare({'query': 'second'})
        relevance = lambda _: {'relevant': True, 'grounded': True}
        grant = await reviewer.verify(prepared=first, relevance=relevance)
        await reviewer.verify(prepared=second, relevance=relevance)
        with pytest.raises(ToolGateError, match='missing'):
            await runner.execute(VerifiedCall(second, grant.gate_id))
        with pytest.raises(ToolGateError, match='missing'):
            await runner.execute(None)
        with pytest.raises(ToolGateError, match='missing'):
            await reviewer.verify(None, relevance=relevance)
        with pytest.raises(ToolGateError, match='missing'):
            await reviewer.verify(replace(first, digest=[]), relevance=relevance)
        assert 'invoke' not in owner.calls
        await runner.execute(verified=grant)
    assert owner.calls.count('invoke') == 1
