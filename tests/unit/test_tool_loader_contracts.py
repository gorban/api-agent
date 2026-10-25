"""Captured tool owners enforce callable contracts and retain failed cleanup."""
import asyncio
import importlib
import sys
from types import SimpleNamespace

import pytest

from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.providers.loading import ProviderError, aload_tool
from api_agent.tooling.admission import capture_tool_factory
from api_agent.workflows.execution import NodeContext, WorkflowError


class Adapter:
    version = 1

    def __init__(self):
        self.events = []

    async def describe(self): return 'description'
    async def validate(self, prepared): return prepared
    async def invoke(self, prepared): return prepared
    async def aclose(self): self.events.append('close')


@pytest.fixture
def captured_factory(tmp_path):
    def capture(source='def create_tool(config, services): return services["adapter"]\n', entry='factory.py'):
        path = tmp_path / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return capture_tool_factory(path, 'create_tool', tmp_path)
    return capture


@pytest.mark.parametrize('version', [None, True, 0, 2, '1'])
@pytest.mark.asyncio
async def test_invalid_version_disposes_acquired_adapter(captured_factory, version):
    adapter = Adapter()
    adapter.version = version
    before = tuple(sys.meta_path)

    with pytest.raises(ProviderError, match='^tool_version_invalid$'):
        await aload_tool(captured_factory(), {}, {'adapter': adapter})

    assert adapter.events == ['close']
    assert tuple(sys.meta_path) == before


@pytest.mark.parametrize('hook', ['describe', 'validate', 'invoke', 'astart'])
@pytest.mark.asyncio
async def test_async_hook_must_accept_contract_arguments(captured_factory, hook):
    adapter = Adapter()
    async def incompatible(first, second): pass
    setattr(adapter, hook, incompatible)

    with pytest.raises(ProviderError, match='^tool_callable_invalid$'):
        await aload_tool(captured_factory(), {}, {'adapter': adapter})

    assert adapter.events == ['close']


@pytest.mark.asyncio
async def test_sync_start_is_rejected_before_startup(captured_factory):
    adapter = Adapter()
    adapter.astart = lambda: pytest.fail('Invalid synchronous startup executed')

    with pytest.raises(ProviderError, match='^tool_callable_invalid$'):
        await aload_tool(captured_factory(), {}, {'adapter': adapter})

    assert adapter.events == ['close']


@pytest.mark.asyncio
async def test_loader_retains_invalid_owner_until_cleanup_retry(captured_factory):
    class BlockedAdapter(Adapter):
        version = 2
        blocked = True
        async def aclose(self):
            self.events.append('close')
            if self.blocked:
                raise ValueError('synthetic close failure')
    adapter = BlockedAdapter()
    before = tuple(sys.meta_path)

    with pytest.raises(ProviderError, match='^tool_version_invalid$') as failure:
        await aload_tool(captured_factory(), {}, {'adapter': adapter})

    owner, = failure.value.retained_owners
    assert str(failure.value.cleanup_error) == 'provider_cleanup_pending'
    assert owner.finder in sys.meta_path
    assert not owner.disposed
    adapter.blocked = False
    await owner.aclose()
    assert adapter.events == ['close', 'close']
    assert owner.disposed
    assert tuple(sys.meta_path) == before
    with pytest.raises(ProviderError, match='^provider_closed$'):
        owner.require_tool()


@pytest.mark.asyncio
async def test_coroutine_factory_result_is_closed_without_running(captured_factory):
    entered = []
    async def result(): entered.append('executed')
    coroutine = result()

    with pytest.raises(ProviderError, match='^tool_factory_must_be_synchronous$'):
        await aload_tool(captured_factory(), {}, {'adapter': coroutine})

    assert coroutine.cr_frame is None
    assert entered == []


@pytest.mark.asyncio
async def test_scheduled_factory_result_is_disposed_before_loader_releases_imports(captured_factory):
    adapter = Adapter()
    result = asyncio.get_running_loop().create_future()
    result.set_result(adapter)
    before = tuple(sys.meta_path)

    with pytest.raises(ProviderError, match='^tool_factory_must_be_synchronous$'):
        await aload_tool(captured_factory(), {}, {'adapter': result})

    assert adapter.events == ['close']
    assert tuple(sys.meta_path) == before


@pytest.mark.parametrize('entry', ['__init__.py', 'nested/__init__.py'])
@pytest.mark.asyncio
async def test_package_factory_imports_are_owned_and_removed(captured_factory, entry):
    adapter = Adapter()
    owner = await aload_tool(captured_factory(entry=entry), {}, {'adapter': adapter})
    assert owner.require_tool() is adapter
    assert owner.module_name in sys.modules
    assert owner.finder in sys.meta_path

    await owner.aclose()

    assert not any(name == owner.module_name or name.startswith(owner.module_name + '.') for name in sys.modules)
    assert owner.finder not in sys.meta_path
    assert adapter.events == ['close']


@pytest.mark.asyncio
async def test_absent_captured_submodule_is_not_loaded_from_disk(captured_factory):
    owner = await aload_tool(captured_factory(), {}, {'adapter': Adapter()})
    try:
        with pytest.raises(ImportError, match='absent from the admitted tool closure'):
            importlib.import_module(owner.module_name + '.absent')
    finally:
        await owner.aclose()


@pytest.mark.parametrize('started,instance', [(False, 'sample/tool'), (True, 'sample/missing')])
def test_runtime_cannot_borrow_unready_or_unknown_tool(started, instance):
    runtime = Runtime('sample')
    if started:
        runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='^runtime_tool_unavailable$'):
        runtime.borrow_tool(instance)
    assert runtime._tool_views == []


@pytest.mark.parametrize('factory', [False, lambda: None])
def test_runtime_rejects_invalid_view_before_it_can_be_invoked(factory):
    adapter = Adapter()
    adapter.for_invocation = factory
    runtime = Runtime('sample', tool_owners={'sample/tool': SimpleNamespace(require_tool=lambda: adapter)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='^runtime_tool_view_invalid$'):
        runtime.borrow_tool('sample/tool')


def test_node_context_resolves_its_named_port_in_node_scope():
    calls = []
    selected = object()
    def resolve(**kwargs):
        calls.append(kwargs)
        return selected
    resolver = SimpleNamespace(for_port=resolve)
    context = NodeContext('read', (), (), {'tools': resolver})

    assert context.tools is resolver
    assert context.tool('primary') is selected
    assert calls == [{'scope_kind': 'node', 'scope': 'read', 'port': 'primary'}]


def test_node_context_without_admission_rejects_tool_access():
    context = NodeContext('read', (), (), {})
    with pytest.raises(WorkflowError, match='^Workflow has no admitted tool bindings$'):
        context.tool('primary')


@pytest.mark.asyncio
async def test_runtime_duplicate_instance_rolls_back_first_owner(captured_factory):
    from test_tool_runtime import Plan, Selected
    adapter = Adapter()
    selected = Selected('sample/tool', captured_factory(), {})
    existing = await aload_tool(selected.admission, {}, {'adapter': adapter})
    runtime = Runtime('sample', tool_plan=Plan((selected,)), tool_owners={'sample/tool': existing},
                      services={'adapter': adapter})

    with pytest.raises(RuntimeLifecycleError) as failure:
        await runtime.astart()

    assert str(failure.value.primary_failure) == 'runtime_tool_duplicate'
    assert adapter.events == ['close']
    assert runtime.pending_cleanup is None
    assert not runtime.ready


@pytest.mark.asyncio
async def test_runtime_acquisition_failure_retains_tool_parent_until_retry(captured_factory):
    from test_tool_runtime import Plan, Selected
    adapter = Adapter()
    adapter.version = 2
    state = {'blocked': True}
    async def close():
        adapter.events.append('tool close')
        if state['blocked']:
            raise RuntimeError('synthetic disposal blocked')
    adapter.aclose = close
    class Parent:
        async def aclose(self): adapter.events.append('parent close')
    runtime = Runtime('sample', tool_plan=Plan((Selected('sample/tool', captured_factory(), {}),)),
                      upstream_provider=Parent(), services={'adapter': adapter})

    with pytest.raises(RuntimeLifecycleError) as failure:
        await runtime.astart()

    assert str(failure.value.primary_failure) == 'tool_version_invalid'
    assert runtime.pending_cleanup is not None
    assert adapter.events == ['tool close', 'tool close']
    assert runtime.tool_owners == {}
    assert len(runtime._retained_owners) == 1
    state['blocked'] = False
    await runtime.aclose()
    assert adapter.events == ['tool close', 'tool close', 'tool close', 'parent close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_invalid_disposer_signature_retains_cleanup_receipt_for_repair(captured_factory):
    adapter = Adapter()
    original_close = adapter.aclose
    async def requires_argument(argument): pass
    adapter.aclose = requires_argument

    with pytest.raises(ProviderError, match='^tool_callable_invalid$') as failure:
        await aload_tool(captured_factory(), {}, {'adapter': adapter})

    owner, = failure.value.retained_owners
    assert not owner.disposed
    adapter.aclose = original_close
    await owner.aclose()
    assert owner.disposed
    assert adapter.events == ['close']


@pytest.mark.asyncio
async def test_absent_factory_result_has_no_owner_left_to_dispose(captured_factory):
    before = tuple(sys.meta_path)
    with pytest.raises(ProviderError, match='^tool_version_invalid$'):
        await aload_tool(captured_factory(), {}, {'adapter': None})
    assert tuple(sys.meta_path) == before


@pytest.mark.parametrize('factory', [None, lambda only_config: None])
@pytest.mark.asyncio
async def test_loader_rechecks_factory_contract_from_import_collaborator(captured_factory, monkeypatch, factory):
    """A module importer returning a broken factory must not bypass runtime checks."""
    from api_agent.providers import loading
    admission = captured_factory()
    imported = []
    actual_signature = loading.inspect.signature
    def signature(value):
        assert value is not None, 'Noncallable factory reached signature inspection'
        return actual_signature(value)
    monkeypatch.setattr(loading.inspect, 'signature', signature)
    original_import = importlib.import_module
    before = tuple(sys.meta_path)
    def import_module(name, package=None):
        if name.startswith('_api_agent_tool_'):
            imported.append(name)
            module = SimpleNamespace(create_tool=factory)
            monkeypatch.setitem(sys.modules, name, module)
            return module
        return original_import(name, package)
    monkeypatch.setattr(importlib, 'import_module', import_module)

    with pytest.raises(ProviderError, match='^tool_factory_invalid$'):
        await aload_tool(admission, {}, {})

    assert len(imported) == 1
    assert imported[0] not in sys.modules
    assert tuple(sys.meta_path) == before


@pytest.mark.asyncio
async def test_loader_normalizes_nested_package_entry_from_admission_collaborator(captured_factory, monkeypatch):
    """Simulate a verified nested entry; ordinary capture currently emits leaf entries."""
    from dataclasses import replace
    from unittest.mock import Mock
    from api_agent.tooling import admission as admission_module
    captured = captured_factory()
    nested = replace(captured, entry='nested/__init__.py')
    verify = admission_module.verify_tool_dependencies
    def verify_selected(value):
        assert value is nested
        verify(captured)
    verifier = Mock(side_effect=verify_selected)
    monkeypatch.setattr(admission_module, 'verify_tool_dependencies', verifier)
    original_import = importlib.import_module
    adapter = Adapter()
    requested = []
    def import_module(name, package=None):
        if name.startswith('_api_agent_tool_'):
            requested.append(name)
            return SimpleNamespace(create_tool=lambda config, services: adapter)
        return original_import(name, package)
    monkeypatch.setattr(importlib, 'import_module', import_module)

    owner = await aload_tool(nested, {}, {})
    try:
        assert owner.require_tool() is adapter
        assert requested == [owner.module_name + '.nested']
        verifier.assert_called_once_with(nested)
    finally:
        await owner.aclose()
    assert adapter.events == ['close']


@pytest.mark.asyncio
async def test_tool_cleanup_tolerates_import_collaborator_already_detaching_finder(captured_factory):
    owner = await aload_tool(captured_factory(), {}, {'adapter': Adapter()})
    sys.meta_path.remove(owner.finder)

    await owner.aclose()

    assert owner.disposed
    assert owner.finder not in sys.meta_path
    assert not any(name == owner.module_name or name.startswith(owner.module_name + '.') for name in sys.modules)


@pytest.mark.asyncio
async def test_import_failure_keeps_primary_when_import_collaborator_detaches_finder(captured_factory, monkeypatch):
    original_import = importlib.import_module
    before = tuple(sys.meta_path)
    failure = ImportError('synthetic importer stopped')
    def import_module(name, package=None):
        if name.startswith('_api_agent_tool_'):
            finder = next(item for item in sys.meta_path if getattr(item, 'namespace', '') == name.rsplit('.', 1)[0])
            sys.meta_path.remove(finder)
            raise failure
        return original_import(name, package)
    monkeypatch.setattr(importlib, 'import_module', import_module)

    with pytest.raises(ImportError) as raised:
        await aload_tool(captured_factory(), {}, {})

    assert raised.value is failure
    assert tuple(sys.meta_path) == before


@pytest.mark.asyncio
async def test_runtime_close_during_tool_acquisition_rolls_back_late_owner(captured_factory, monkeypatch):
    from api_agent.providers import loading
    from test_tool_runtime import Plan, Selected
    acquired, release = asyncio.Event(), asyncio.Event()
    adapter = Adapter()
    actual_load = loading.aload_tool
    async def delayed_load(*args):
        owner = await actual_load(*args)
        acquired.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Simulate a collaborator that finishes acquisition while draining.
            await release.wait()
        return owner
    monkeypatch.setattr(loading, 'aload_tool', delayed_load)
    runtime = Runtime('sample', tool_plan=Plan((Selected('sample/tool', captured_factory(), {}),)),
                      services={'adapter': adapter})
    starting = asyncio.create_task(runtime.astart())
    await acquired.wait()
    closing = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(RuntimeLifecycleError) as failure:
        await starting
    await closing

    assert str(failure.value.primary_failure) == 'runtime_closed'
    assert adapter.events == ['close']
    assert not runtime.ready
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_runtime_close_during_first_tool_start_skips_later_start(captured_factory):
    from test_tool_runtime import Plan, Selected
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = Adapter(), Adapter()
    async def first_start():
        first.events.append('start')
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Startup may finish after cancellation; the next owner must not start.
            await release.wait()
    async def second_start(): second.events.append('start')
    first.astart, second.astart = first_start, second_start
    captured = captured_factory('def create_tool(config, services): return services["adapters"][config["ID"]]\n')
    runtime = Runtime('sample', tool_plan=Plan((Selected('sample/first', captured, {'ID': 'first'}),
        Selected('sample/second', captured, {'ID': 'second'}))), services={'adapters': {'first': first, 'second': second}})
    starting = asyncio.create_task(runtime.astart())
    await entered.wait()
    closing = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(RuntimeLifecycleError) as failure:
        await starting
    await closing

    assert str(failure.value.primary_failure) == 'runtime_closed'
    assert first.events == ['start', 'close']
    assert second.events == ['close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_pending_studio_cleanup_retains_session_until_retry():
    from api_agent.providers.control import CleanupPendingError
    events = []
    class Backend:
        blocked = True
        async def aclose(self):
            events.append('studio close')
            if self.blocked:
                raise RuntimeError('synthetic studio cleanup blocked')
    class Sessions:
        async def aclose(self): events.append('sessions close')
    backend, sessions = Backend(), Sessions()
    runtime = Runtime('sample', sessions=sessions, services={'studio_backend': backend})
    runtime.start()

    with pytest.raises(CleanupPendingError) as failure:
        await runtime.aclose()

    assert failure.value.owners == (backend, sessions)
    assert events == ['studio close']
    backend.blocked = False
    await runtime.aclose()
    assert events == ['studio close', 'studio close', 'sessions close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_runtime_rejects_async_view_factory_without_running_it():
    adapter = Adapter()
    async def for_invocation():
        adapter.events.append('factory executed')
        return Adapter()
    adapter.for_invocation = for_invocation
    runtime = Runtime('sample', tool_owners={'sample/tool': SimpleNamespace(
        require_tool=lambda: adapter, aclose=adapter.aclose)})
    runtime.start()

    with pytest.raises(RuntimeLifecycleError, match='^runtime_tool_view_invalid$'):
        runtime.borrow_tool('sample/tool')
    await runtime.aclose()

    assert adapter.events == ['close']
    assert runtime._tool_views == []


@pytest.mark.asyncio
async def test_tool_finder_delegates_external_import_to_receipt_owner(captured_factory, monkeypatch):
    from unittest.mock import Mock
    from api_agent.tooling import admission as admission_module
    captured = captured_factory()
    owner = await aload_tool(captured, {}, {'adapter': Adapter()})
    receipt_spec = object()
    dependency = Mock(return_value=receipt_spec)
    monkeypatch.setattr(admission_module, 'admitted_dependency_spec', dependency)
    try:
        assert owner.finder.find_spec('synthetic_dependency', ('owned-path',)) is receipt_spec
        dependency.assert_called_once_with(captured, 'synthetic_dependency', ('owned-path',))
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_pending_service_registry_retains_upstream_until_retry():
    from api_agent.providers.control import CleanupPendingError
    events = []
    class Registry:
        blocked = True
        async def aclose(self):
            events.append('registry close')
            if self.blocked:
                raise RuntimeError('synthetic registry cleanup blocked')
    class Upstream:
        async def aclose(self): events.append('upstream close')
    registry, upstream = Registry(), Upstream()
    runtime = Runtime('sample', upstream_provider=upstream, services={'service_registry': registry})
    runtime.start()

    with pytest.raises(CleanupPendingError) as failure:
        await runtime.aclose()

    assert failure.value.owners == (registry, upstream)
    assert events == ['registry close']
    registry.blocked = False
    await runtime.aclose()
    assert events == ['registry close', 'registry close', 'upstream close']
    assert runtime.pending_cleanup is None
