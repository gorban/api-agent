"""Named tool owners join the existing Runtime lifecycle."""
from dataclasses import dataclass, field, replace
from types import MappingProxyType

import pytest

from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.tooling.admission import capture_tool_factory


SOURCE = '''from dataclasses import dataclass
from .sibling import marker
@dataclass
class Owner:
    config: object
    version = 1
    async def describe(self): return marker
    async def validate(self, prepared): pass
    async def invoke(self, prepared): return self.config['VALUE']
    async def aclose(self): pass
def create_tool(local_config, services): return Owner(local_config)
'''


@dataclass(frozen=True)
class Selected:
    id: str
    admission: object
    local_values: object
    interface: str = 'verified-read@1'
    pending: object = ()
    service_bindings: object = ()
    service_capabilities: object = ()


@dataclass(frozen=True)
class Plan:
    tools: tuple
    bindings: object = field(default_factory=dict)
    definitions: tuple = ()


@pytest.fixture
def admitted(tmp_path):
    factory = tmp_path / 'factory.py'
    factory.write_text(SOURCE)
    (tmp_path / 'sibling.py').write_text('marker = "captured"\n')
    return capture_tool_factory(factory, 'create_tool', tmp_path)


@pytest.mark.asyncio
async def test_two_same_factory_instances_keep_separate_runtime_owners(admitted):
    plan = Plan((Selected('demo/first', admitted, {'VALUE': 'first'}),
                 Selected('demo/second', admitted, {'VALUE': 'second'})))
    runtime = Runtime('demo', tool_plan=plan)
    await runtime.astart()
    try:
        first, second = runtime.borrow_tool('demo/first'), runtime.borrow_tool('demo/second')
        assert await first.invoke(None) == 'first'
        assert await second.invoke(None) == 'second'
        assert first.owner is not second.owner
        assert runtime.api_provider is None
    finally:
        await runtime.aclose()
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_loader_uses_captured_sibling_after_source_changes(admitted, tmp_path):
    (tmp_path / 'sibling.py').write_text('raise RuntimeError("changed source")\n')
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admitted, {}),)))
    await runtime.astart()
    try:
        assert await runtime.borrow_tool('demo/first').describe() == 'captured'
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_invalid_callable_is_rejected_and_primary_failure_retained(tmp_path):
    factory = tmp_path / 'factory.py'
    factory.write_text(SOURCE.replace('async def validate', 'def validate'))
    (tmp_path / 'sibling.py').write_text('marker = "captured"\n')
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admission, {}),)))

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert str(error.value.primary_failure) == 'tool_callable_invalid'
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_pending_output_rejects_every_factory_before_import(admitted):
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admitted, {},
        pending={'ENDPOINT': {'output': 'demo/api/http'}}),)))

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert 'ready outputs' in str(error.value.primary_failure)
    assert runtime.tool_owners == {}


@pytest.mark.asyncio
async def test_failed_start_preserves_primary_and_retains_parent_until_retry(tmp_path):
    source = '''class Owner:
    version = 1
    def __init__(self, services): self.services = services
    async def astart(self): raise ValueError('synthetic startup failure')
    async def describe(self): pass
    async def validate(self, prepared): pass
    async def invoke(self, prepared): pass
    async def aclose(self):
        self.services['events'].append('tool close')
        if self.services['state']['blocked']: raise RuntimeError('cleanup blocked')
def create_tool(config, services): return Owner(services)
'''
    factory = tmp_path / 'factory.py'
    factory.write_text(source)
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    events, state = [], {'blocked': True}

    class Parent:
        async def aclose(self):
            events.append('parent close')

    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admission, {}),)),
                      upstream_provider=Parent(), services={'events': events, 'state': state})

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert str(error.value.primary_failure) == 'synthetic startup failure'
    assert runtime.pending_cleanup is not None
    assert events == ['tool close']
    assert len(runtime.tool_owners) == 1
    state['blocked'] = False
    await runtime.aclose()
    assert events == ['tool close', 'tool close', 'parent close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_pending_borrowed_view_retains_tool_before_retry(tmp_path):
    source = '''class View:
    def __init__(self, services): self.services = services
    async def describe(self): pass
    async def validate(self, prepared): pass
    async def invoke(self, prepared): pass
    async def aclose(self):
        self.services['events'].append('view close')
        if self.services['state']['blocked']: raise RuntimeError('view blocked')
class Owner(View):
    version = 1
    def for_invocation(self): return View(self.services)
    async def aclose(self): self.services['events'].append('tool close')
def create_tool(config, services): return Owner(services)
'''
    factory = tmp_path / 'factory.py'
    factory.write_text(source)
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    events, state = [], {'blocked': True}
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admission, {}),)),
                      services={'events': events, 'state': state})
    await runtime.astart()
    view = runtime.borrow_tool('demo/first')

    from api_agent.providers.control import CleanupPendingError
    with pytest.raises(CleanupPendingError):
        await runtime.aclose()

    assert events == ['view close']
    assert not view.closed
    state['blocked'] = False
    await runtime.aclose()
    assert events == ['view close', 'view close', 'tool close']
    assert view.closed


def test_port_lookup_keeps_graph_and_node_scopes_distinct_and_rejects_unknown_scope():
    from types import SimpleNamespace
    from api_agent.providers.contracts import ContractError
    definition = SimpleNamespace(definition_id='definition', graph_id='same',
        layout=SimpleNamespace(steps=(SimpleNamespace(id='same', kind='node', parent=None),)))
    bindings = {('definition', 'graph', 'same', 'primary'): 'demo/root',
                ('definition', 'node', 'same', 'local'): 'demo/node'}
    runtime = Runtime('demo', tool_plan=Plan((), bindings, (definition,)))
    tools = runtime.tools_for(definition)

    assert tools.instance(scope_kind='node', scope='same', port='primary') == 'demo/root'
    assert tools.instance(scope_kind='node', scope='same', port='local') == 'demo/node'
    with pytest.raises(ContractError, match='port is unavailable'):
        tools.instance(scope_kind='graph', scope='same', port='local')
    with pytest.raises(ContractError, match='scope is unavailable'):
        tools.instance(scope_kind='node', scope='unknown', port='primary')


def test_unadmitted_workflow_cannot_borrow_another_graph_binding():
    from types import SimpleNamespace
    from api_agent.providers.contracts import ContractError
    runtime = Runtime('demo', tool_plan=Plan(()))

    with pytest.raises(ContractError, match='no admitted tool plan'):
        runtime.tools_for(SimpleNamespace(definition_id='unadmitted'))


@pytest.mark.asyncio
async def test_runtime_drains_active_borrowed_call_before_disposing_owner(tmp_path):
    import asyncio
    source = '''import asyncio
class Owner:
    version = 1
    def __init__(self, services): self.services = services
    async def describe(self): pass
    async def validate(self, prepared): pass
    async def invoke(self, prepared):
        self.services['entered'].set()
        try: await asyncio.Event().wait()
        finally: self.services['events'].append('call settled')
    async def aclose(self): self.services['events'].append('owner close')
def create_tool(config, services): return Owner(services)
'''
    factory = tmp_path / 'factory.py'
    factory.write_text(source)
    admission = capture_tool_factory(factory, 'create_tool', tmp_path)
    entered, events = asyncio.Event(), []
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', admission, {}),)),
                      services={'entered': entered, 'events': events})
    await runtime.astart()
    call = asyncio.create_task(runtime.borrow_tool('demo/first').invoke(None))
    await entered.wait()

    await runtime.aclose()

    with pytest.raises(asyncio.CancelledError):
        await call
    assert events == ['call settled', 'owner close']


@pytest.mark.asyncio
async def test_forged_captured_source_cannot_reuse_admission_identity(admitted):
    forged = replace(admitted, sources={**admitted.sources, 'sibling.py': b'marker = "forged"\n'})
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', forged, {}),)))

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert 'admission identity changed' in str(error.value.primary_failure)
    assert runtime.tool_owners == {}


def test_discovery_captures_optional_requirements_without_installed_packages(tmp_path, monkeypatch):
    from api_agent.tooling import admission
    factory = tmp_path / 'factory.py'
    factory.write_text('import missing_optional_tool_library\ndef create_tool(config, services): pass\n')
    manifest = tmp_path / 'provider-dependencies.toml'
    manifest.write_text('version=1\ndependencies=["missing-optional-tool-library==1.0"]\n')
    monkeypatch.setattr(admission.metadata, 'packages_distributions',
                        lambda: pytest.fail('Discovery inspected installed tool requirements'))

    captured = admission.capture_tool_factory(factory, 'create_tool', tmp_path)

    assert captured.dependencies == (('missing_optional_tool_library', None),)
    assert captured.assets[manifest] == manifest.read_bytes()


@pytest.mark.asyncio
async def test_unprepared_optional_packages_are_actionable_before_any_factory_import(tmp_path, monkeypatch):
    from api_agent.tooling import admission
    factory = tmp_path / 'factory.py'
    factory.write_text('import first_optional_library\nimport second_optional_library\n'
                       'raise AssertionError("factory imported before dependencies")\n'
                       'def create_tool(config, services): pass\n')
    captured = admission.capture_tool_factory(factory, 'create_tool', tmp_path)
    monkeypatch.setattr(admission.metadata, 'packages_distributions', lambda: {})
    runtime = Runtime('demo', tool_plan=Plan((Selected('demo/first', captured, {}),)))

    with pytest.raises(RuntimeLifecycleError) as error:
        await runtime.astart()

    assert 'Missing tool dependencies: first_optional_library, second_optional_library' in str(error.value.primary_failure)
    assert runtime.tool_owners == {}


@pytest.mark.parametrize('view', [None, object()])
@pytest.mark.asyncio
async def test_rejected_view_without_disposer_does_not_retain_parent(view):
    from types import SimpleNamespace
    events = []

    class Owner:
        def for_invocation(self): return view
        async def aclose(self): events.append('owner close')

    owner = Owner()
    loaded = SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)
    runtime = Runtime('demo', tool_owners={'demo/first': loaded})
    runtime.start()

    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')
    await runtime.aclose()

    assert events == ['owner close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_rejected_view_with_failing_disposer_retains_parent_for_retry():
    from types import SimpleNamespace
    from api_agent.providers.control import CleanupPendingError
    events, state = [], {'blocked': True}

    class InvalidView:
        async def aclose(self):
            events.append('view close')
            if state['blocked']: raise RuntimeError('view cleanup blocked')

    class Owner:
        def for_invocation(self): return InvalidView()
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')

    with pytest.raises(CleanupPendingError):
        await runtime.aclose()
    assert events == ['view close']
    state['blocked'] = False
    await runtime.aclose()
    assert events == ['view close', 'view close', 'owner close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_successful_repeated_views_release_runtime_tracking_and_objects():
    import asyncio
    import gc
    import weakref
    from types import SimpleNamespace

    class View:
        async def describe(self): pass
        async def validate(self, prepared): pass
        async def invoke(self, prepared): pass
        async def aclose(self): pass

    class Owner(View):
        def for_invocation(self): return View()

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    references = []
    for _ in range(32):
        view = runtime.borrow_tool('demo/first')
        references.extend((weakref.ref(view), weakref.ref(view.view)))
        await view.aclose()
        assert runtime._tool_views == []
        del view
    await asyncio.sleep(0)
    gc.collect()

    assert all(reference() is None for reference in references)
    await runtime.aclose()


@pytest.mark.asyncio
async def test_rejected_view_with_sync_disposer_is_cleaned_before_parent():
    from types import SimpleNamespace
    events = []

    class InvalidView:
        def aclose(self): events.append('view close')

    class Owner:
        def for_invocation(self): return InvalidView()
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')

    await runtime.aclose()

    assert events == ['view close', 'owner close']


@pytest.mark.asyncio
async def test_rejected_coroutine_view_is_closed_without_running_its_body():
    import inspect
    from types import SimpleNamespace
    events, returned = [], []

    async def invalid_view(): events.append('invalid view executed')

    class Owner:
        def for_invocation(self):
            view = invalid_view()
            returned.append(view)
            return view
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')

    await runtime.aclose()

    assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
    assert events == ['owner close']


@pytest.mark.asyncio
async def test_slow_view_close_stays_tracked_and_shared_until_success():
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.control import CleanupPendingError
    events, release = [], asyncio.Event()

    class View:
        async def describe(self): return 'selected'
        async def validate(self, prepared): pass
        async def invoke(self, prepared): pass
        async def aclose(self):
            events.append('view close')
            await release.wait()

    class Owner(View):
        def for_invocation(self): return View()
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', timeout=.01,
        tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    view = runtime.borrow_tool('demo/first')

    outcomes = await asyncio.gather(view.aclose(), view.aclose(), return_exceptions=True)

    assert all(isinstance(outcome, CleanupPendingError) for outcome in outcomes)
    assert events == ['view close']
    assert runtime._tool_views == [view]
    with pytest.raises(ContractError, match='invocation is closed'):
        await view.describe()
    release.set()
    await view.aclose()
    await view.aclose()
    assert runtime._tool_views == []
    await runtime.aclose()
    assert events == ['view close', 'owner close']


@pytest.mark.asyncio
async def test_nested_scope_resolves_nearest_declared_ancestor_without_parent_ownership():
    from types import SimpleNamespace
    definition = SimpleNamespace(definition_id='definition', graph_id='graph', layout=SimpleNamespace(steps=(
        SimpleNamespace(id='outer', kind='component', parent=None),
        SimpleNamespace(id='inner', kind='component', parent='outer'),
        SimpleNamespace(id='read', kind='node', parent='inner'))))
    bindings = {('definition', 'component', 'outer', 'primary'): 'demo/first'}
    events = []

    class Owner:
        async def describe(self): return 'ancestor selected'
        async def validate(self, prepared): pass
        async def invoke(self, prepared): pass
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_plan=Plan((), bindings, (definition,)),
        tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    tools = runtime.tools_for(definition)
    view = tools.for_port(scope_kind='node', scope='read', port='primary')
    assert await view.describe() == 'ancestor selected'
    await view.aclose()
    assert events == []
    await runtime.aclose()
    assert events == ['owner close']


@pytest.mark.asyncio
async def test_rejected_scheduled_view_retains_and_closes_its_returned_owner():
    import asyncio
    from types import SimpleNamespace
    events = []

    class InvalidView:
        async def aclose(self): events.append('returned view close')

    future = asyncio.get_running_loop().create_future()
    future.set_result(InvalidView())

    class Owner:
        def for_invocation(self): return future
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')

    await runtime.aclose()

    assert events == ['returned view close', 'owner close']
    assert runtime.pending_cleanup is None


@pytest.mark.asyncio
async def test_rejected_scheduled_view_keeps_pending_result_disposer_for_retry():
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.control import CleanupPendingError
    events, state = [], {'blocked': True}

    class InvalidView:
        async def aclose(self):
            events.append('returned view close')
            if state['blocked']: raise RuntimeError('result cleanup blocked')

    future = asyncio.get_running_loop().create_future()
    future.set_result(InvalidView())

    class Owner:
        def for_invocation(self): return future
        async def aclose(self): events.append('owner close')

    owner = Owner()
    runtime = Runtime('demo', tool_owners={'demo/first': SimpleNamespace(require_tool=lambda: owner, aclose=owner.aclose)})
    runtime.start()
    with pytest.raises(RuntimeLifecycleError, match='runtime_tool_view_invalid'):
        runtime.borrow_tool('demo/first')
    with pytest.raises(CleanupPendingError):
        await runtime.aclose()
    assert events == ['returned view close']
    state['blocked'] = False
    await runtime.aclose()
    assert events == ['returned view close', 'returned view close', 'owner close']
    assert runtime._tool_views == []
