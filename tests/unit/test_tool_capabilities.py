"""Explicit capability binding never gives a consumer parent disposal rights."""
import asyncio

import pytest

from api_agent.tooling.capabilities import ServiceBinding, ServiceRegistry


class Parent:
    def __init__(self):
        self.calls, self.blocked = 0, False

    async def aclose(self):
        self.calls += 1
        if self.blocked:
            raise RuntimeError('cleanup blocked')


def binding(parent=None):
    return ServiceBinding(parent or Parent(), {'read@1': object()})


def test_explicit_view_map_is_immutable_and_cannot_dispose_parent():
    parent, view = Parent(), object()
    item = ServiceBinding(parent, {'read@1': view})
    registry = ServiceRegistry({'fixture/store': item})
    views = registry.bind({'store': 'fixture/store'}, {'store': 'read@1'})
    assert views['store'] is view
    assert views['store'] is not parent
    with pytest.raises(TypeError):
        views['store'] = parent
    with pytest.raises(TypeError):
        item.capabilities['write@1'] = parent


@pytest.mark.parametrize('capabilities', [[], {}, {'read': object()}, {1: object()}])
def test_binding_rejects_invalid_capability_declarations(capabilities):
    with pytest.raises(ValueError):
        ServiceBinding(Parent(), capabilities)


@pytest.mark.parametrize('method', ['close', 'aclose', 'dispose', 'adispose', 'release'])
def test_view_with_disposal_method_is_rejected(method):
    view = type('DisposingView', (), {method: lambda self: None})()
    with pytest.raises(ValueError, match='non-owning'):
        ServiceBinding(Parent(), {'read@1': view})


def test_parent_cannot_be_its_own_view():
    parent = object()
    with pytest.raises(ValueError, match='non-owning'):
        ServiceBinding(parent, {'read@1': parent})


@pytest.mark.parametrize('bindings', [[], {'invalid': binding()}, {1: binding()}, {'fixture/store': object()}])
def test_registry_rejects_unqualified_or_untyped_bindings(bindings):
    with pytest.raises(ValueError, match='qualified'):
        ServiceRegistry(bindings)


@pytest.mark.parametrize('slots,requirements', [([], {}), ({}, []), ({'x': 'fixture/store'}, {}),
    ({1: 'fixture/store'}, {1: 'read@1'}), ({'x': 1}, {'x': 'read@1'}),
    ({'': 'fixture/store'}, {'': 'read@1'}), ({'x': 'fixture/store'}, {'x': 1}),
    ({'x': 'fixture/store'}, {'x': 'read'}), ({'x': 'fixture/missing'}, {'x': 'read@1'}),
    ({'x': 'fixture/store'}, {'x': 'write@1'})])
def test_bind_rejects_mismatched_or_unavailable_capabilities(slots, requirements):
    with pytest.raises(ValueError):
        ServiceRegistry({'fixture/store': binding()}).bind(slots, requirements)


@pytest.mark.asyncio
async def test_shared_parent_is_disposed_once_across_aliases_and_repeated_cleanup():
    parent = Parent()
    item = binding(parent)
    registry = ServiceRegistry({'fixture/first': item, 'fixture/second': item})
    await registry.aclose()
    await registry.aclose()
    assert parent.calls == 1
    with pytest.raises(ValueError, match='closing'):
        registry.bind({}, {})


@pytest.mark.asyncio
async def test_cleanup_failure_retains_only_failed_parent_for_retry():
    completed, failed = Parent(), Parent()
    failed.blocked = True
    registry = ServiceRegistry({'fixture/failed': binding(failed), 'fixture/completed': binding(completed)})
    with pytest.raises(ExceptionGroup) as failure:
        await registry.aclose()
    assert str(failure.value.exceptions[0]) == 'cleanup blocked'
    assert (completed.calls, failed.calls) == (1, 1)
    failed.blocked = False
    await registry.aclose()
    assert (completed.calls, failed.calls) == (1, 2)


@pytest.mark.asyncio
async def test_nonawaitable_close_and_no_close_handles_are_supported():
    events = []
    sync = type('Parent', (), {'aclose': lambda self: events.append('closed')})()
    registry = ServiceRegistry({'fixture/sync': binding(sync), 'fixture/plain': binding(object())})
    await registry.aclose()
    assert events == ['closed']


def test_registry_rejects_another_event_loop():
    registry = ServiceRegistry({})
    asyncio.run(registry.aclose())
    with pytest.raises(ValueError, match='another event loop'):
        asyncio.run(registry.aclose())


@pytest.mark.asyncio
async def test_cancelled_waiter_retains_one_inflight_parent_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowParent(Parent):
        async def aclose(self):
            self.calls += 1
            entered.set()
            await release.wait()

    parent = SlowParent()
    registry = ServiceRegistry({'fixture/store': binding(parent)})
    waiter = asyncio.create_task(registry.aclose())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await registry.aclose()
    assert parent.calls == 1
