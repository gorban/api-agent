"""Generic contract v1 loading and composition, with synthetic local adapters."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import sys

import pytest

from api_agent.api.application import Runtime, RuntimeLifecycleError
from api_agent.api.composition import compose_runtime
from api_agent.config import load_shared_schema
from api_agent.config.declarations import ConfigSchema, FieldDeclaration
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.loading import (ProviderError, bootstrap_schema, load_provider,
                                         validate_capabilities)


def source(root, body, declaration=''):
    root.mkdir(exist_ok=True, parents=True)
    (root / 'provider.py').write_text(body)
    if declaration:
        (root / 'cfg').mkdir()
        (root / 'cfg/config.toml').write_text('version=1\n' + declaration)
    return str(root / 'provider.py') + ':create'


def body(kind, operation, method, *, routes=''):
    return f'''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version = 1
    capabilities = ProviderCapabilities({kind!r}, ({operation!r},))
    def __init__(self): self.calls = []
    async def {method}(self{', request, *, auth=None' if kind in ('api', 'upstream') else ''}): pass
    async def aclose(self): self.calls.append('close')
    {routes}
def create(config, services): return Adapter()
'''


def test_local_relative_imports_are_isolated_and_removed(tmp_path):
    location = source(tmp_path, 'from .helper import VALUE\nclass Adapter: version=1\ndef create(config, services): return Adapter()\n')
    (tmp_path / 'helper.py').write_text('VALUE=7\n')
    before = list(sys.path)
    owner = load_provider(location, {}, {})
    assert owner.module_name + '.helper' in sys.modules
    assert sys.path == before
    asyncio.run(owner.aclose())
    assert not any(key.startswith(owner.module_name) for key in sys.modules)


def test_failed_relative_import_removes_entire_namespace(tmp_path):
    location = source(tmp_path, 'from .helper import VALUE\nraise RuntimeError("private")\n')
    (tmp_path / 'helper.py').write_text('VALUE=7\n')
    before = set(sys.modules)
    with pytest.raises(ProviderError, match='initialization_failed'):
        load_provider(location, {}, {})
    assert set(sys.modules) == before


def test_api_contributes_nested_owner_relative_selection_without_import(tmp_path):
    upstream = source(tmp_path / 'upstream', 'raise AssertionError()\n', '[fields.EXTRA]\ntype="int"\ndefault=4\n')
    api = source(tmp_path / 'api', 'raise AssertionError()\n', '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\ndefault="../upstream/provider.py:create"\n')
    base = ConfigSchema({'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=api)})
    schema = bootstrap_schema(base, ['--extra=8'], {}, {}, owner_root=tmp_path)
    assert schema.fields['EXTRA'].default == 4
    assert schema.fields['UPSTREAM_API_PROVIDER'].default == upstream
    with pytest.raises(ProviderError, match='cycle'):
        bootstrap_schema(base, [], {'UPSTREAM_API_PROVIDER': api}, {})


def test_nested_selection_cannot_rebind_its_parent(tmp_path):
    api = source(tmp_path, 'raise AssertionError()\n', '[fields.API_PROVIDER]\ntype="str"\ndefault=""\n')
    base = ConfigSchema({'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=api)})
    with pytest.raises(ProviderError, match='cycle'):
        bootstrap_schema(base, [], {}, {})


@pytest.mark.parametrize('capabilities', [
    object(), ProviderCapabilities('unknown'), ProviderCapabilities('api', version=True),
    ProviderCapabilities('api', operations=['invoke']),
    ProviderCapabilities('api', ('invoke', 'invoke')),
    ProviderCapabilities('api', ('unknown',)),
    ProviderCapabilities('api', credential_roles=['role']),
    ProviderCapabilities('api', credential_roles=('',)),
    ProviderCapabilities('api', credential_roles=('role', 'role')),
    ProviderCapabilities('api', llm_role='role'),
    ProviderCapabilities('llm', llm_role='missing'),
])
def test_malformed_capabilities_are_rejected(capabilities):
    with pytest.raises(ProviderError, match='capability_invalid'):
        validate_capabilities(SimpleNamespace(capabilities=capabilities))


@pytest.mark.parametrize('method', ['def ainvoke(self, request, *, auth=None): pass',
                                   'async def ainvoke(self): pass',
                                   'ainvoke = None'])
def test_advertised_async_signature_is_required(tmp_path, method):
    location = source(tmp_path, f'from api_agent.providers.contracts import ProviderCapabilities\nclass Adapter:\n    version=1\n    capabilities=ProviderCapabilities("api", ("invoke",))\n    {method}\ndef create(config, services): return Adapter()\n')
    with pytest.raises(ProviderError, match='initialization_failed'):
        load_provider(location, {}, {}, kind='api')


def test_stream_and_empty_capabilities_and_kind_validation():
    class Adapter:
        capabilities = ProviderCapabilities('llm', ('stream',), ('role',), 'role')
        async def astream(self, request, *, auth=None):
            yield request
    validate_capabilities(Adapter(), 'llm')
    async def consume():
        assert [value async for value in Adapter().astream('synthetic')] == ['synthetic']
    asyncio.run(consume())
    with pytest.raises(ProviderError):
        validate_capabilities(Adapter(), 'api')
    validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities('ui')))
    validate_capabilities(object())


def test_version_only_foundation_does_not_claim_operation(tmp_path):
    owner = load_provider(source(tmp_path, 'class Adapter: version=1\ndef create(config, services): return Adapter()\n'), {}, {})
    runtime = Runtime('synthetic', llm_provider=owner)
    with pytest.raises(ProviderError, match='operation_unavailable'):
        runtime.require_llm_provider()
    with pytest.raises(ProviderError, match='location_required'):
        runtime.require_provider('api', 'invoke')
    asyncio.run(runtime.aclose())
    with pytest.raises(ProviderError, match='provider_closed'):
        owner.require('llm', 'complete')
    with pytest.raises(RuntimeLifecycleError, match='closed'):
        runtime.require_provider('llm', 'complete')


def test_all_owners_compose_with_immutable_services_and_close(tmp_path):
    api = source(tmp_path / 'api', body('api', 'invoke', 'ainvoke'))
    ui = source(tmp_path / 'ui', body('ui', 'launch', 'astart'))
    upstream = source(tmp_path / 'upstream', body('upstream', 'validate', 'avalidate'))
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', owner_root=tmp_path, identity_factory_path=None),
                              {'API_PROVIDER': api, 'UI_PROVIDER': ui, 'UPSTREAM_API_PROVIDER': upstream}, callback_origin='https://example.test')
    assert runtime.services['ssl_context'].check_hostname
    assert runtime.services['tls_material'] is None
    with pytest.raises(TypeError):
        runtime.services['owner_root'] = tmp_path
    async def scenario():
        await runtime.astart()
        assert runtime.require_provider('api', 'invoke') is runtime.api_provider.implementation
        with pytest.raises(ProviderError):
            runtime.require_provider('api', 'other')
        await runtime.aclose()
    asyncio.run(scenario())
    assert all(owner.closed for owner in (runtime.api_provider, runtime.ui_provider, runtime.upstream_provider))


def test_async_composition_defers_factories_and_awaits_failed_start_cleanup(tmp_path):
    api = source(tmp_path / 'api', body('api', 'invoke', 'ainvoke'))
    failing = source(tmp_path / 'ui', 'def create(config, services): raise RuntimeError("private")\n')
    async def scenario():
        runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', identity_factory_path=None),
                                  {'API_PROVIDER': api, 'UI_PROVIDER': failing}, callback_origin='https://example.test')
        assert runtime.api_provider is None
        with pytest.raises(RuntimeLifecycleError, match='start_failed'):
            await runtime.astart()
        assert runtime.api_provider.closed
    asyncio.run(scenario())


def test_async_composition_success_and_factory_rollback(tmp_path):
    api = source(tmp_path / 'api', body('api', 'invoke', 'ainvoke'))
    context = SimpleNamespace(pack_id='synthetic', identity_factory_path=None)
    async def scenario():
        runtime = compose_runtime(context, {'API_PROVIDER': api}, callback_origin='https://example.test')
        await runtime.astart()
        assert runtime.require_provider('api', 'invoke')
        await runtime.aclose()
    asyncio.run(scenario())
    broken = source(tmp_path / 'broken', 'def create(config, services): raise RuntimeError()\n')
    before = set(sys.modules)
    runtime = compose_runtime(context, {'API_PROVIDER': api, 'UI_PROVIDER': broken}, callback_origin='https://example.test')
    with pytest.raises(RuntimeLifecycleError):
        asyncio.run(runtime.astart())
    assert set(sys.modules) == before


def test_contributed_selection_with_no_default_is_optional(tmp_path):
    api = source(tmp_path / 'api', 'raise AssertionError()\n', '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\n')
    declaration = tmp_path / 'contribution.toml'
    declaration.write_text('version=1\n[fields.UNRELATED]\ntype="int"\ndefault=3\n[fields.API_PROVIDER]\ntype="str"\ndefault=' + repr(api).replace("'", '"') + '\n')
    schema = bootstrap_schema(ConfigSchema({}), [], {}, {}, owner_root=tmp_path, configuration_paths=(declaration,))
    assert 'UPSTREAM_API_PROVIDER' in schema.fields


def test_nested_default_can_be_explicitly_empty(tmp_path):
    api = source(tmp_path, 'raise AssertionError()\n', '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\ndefault=""\n')
    schema = bootstrap_schema(ConfigSchema({'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=api)}), [], {}, {})
    assert schema.fields['UPSTREAM_API_PROVIDER'].default == ''


def test_nested_upstream_routes_get_its_configuration_owner(tmp_path):
    upstream = source(tmp_path / 'api' / 'nested', body('upstream', 'validate', 'avalidate', routes='def setup_routes(self, registrar, services): self.root = services["owner_root"]'))
    api = source(tmp_path / 'api', body('api', 'invoke', 'ainvoke'))
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', owner_root=tmp_path, identity_factory_path=None),
                              {'API_PROVIDER': api, 'UPSTREAM_API_PROVIDER': 'nested/provider.py:create'}, callback_origin='https://example.test')
    async def scenario():
        await runtime.astart()
        assert runtime.upstream_provider.implementation.root == tmp_path / 'api'
        await runtime.aclose()
    asyncio.run(scenario())


def test_provider_routes_collision_closes_all_started_and_unstarted_owners(tmp_path):
    calls = []
    setup = 'def setup_routes(self, registrar, services): registrar.add("/synthetic", ("GET",), lambda request: None)'
    api = source(tmp_path / 'api', body('api', 'invoke', 'ainvoke', routes=setup))
    ui = source(tmp_path / 'ui', body('ui', 'launch', 'astart', routes=setup))
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', identity_factory_path=None),
                              {'API_PROVIDER': api, 'UI_PROVIDER': ui}, callback_origin='https://example.test')
    with pytest.raises(RuntimeLifecycleError, match='start_failed'):
        asyncio.run(runtime.astart())
    assert runtime.api_provider.closed and runtime.ui_provider.closed
    assert not runtime.ready


def test_close_order_follows_provider_dependency_order():
    calls = []
    def owner(name):
        async def close(): calls.append(name)
        return SimpleNamespace(aclose=close)
    runtime = Runtime('synthetic', ui_provider=owner('ui'), api_provider=owner('api'),
                      upstream_provider=owner('upstream'), llm_provider=owner('llm'))
    asyncio.run(runtime.aclose())
    assert calls == ['ui', 'llm', 'api', 'upstream']


@pytest.mark.parametrize('close_body', ['self.services["calls"].append("closed")', 'raise RuntimeError("private")'])
def test_invalid_factory_result_is_disposed_before_async_failure(tmp_path, close_body):
    from api_agent.providers.loading import aload_provider
    location = source(tmp_path, f'class Adapter:\n    version=2\n    def __init__(self, services): self.services=services\n    async def aclose(self): {close_body}\ndef create(config, services): return Adapter(services)\n')
    from unittest.mock import AsyncMock
    calls = []
    before = set(sys.modules)
    async def scenario():
        with pytest.raises(ProviderError) as error:
            await aload_provider(location, {}, {'calls': calls})
        if close_body.startswith('raise'):
            retained = error.value.retained_owners
            assert len(retained) == 1 and retained[0].module_name in sys.modules
            retained[0].implementation.aclose = AsyncMock()
            await retained[0].aclose()
    asyncio.run(scenario())
    assert calls == ([] if close_body.startswith('raise') else ['closed'])
    assert set(sys.modules) == before
    with pytest.raises(ProviderError) as error:
        load_provider(location, {}, {'calls': calls})
    retained = error.value.retained_owners[0]
    assert retained._loop is None and retained.module_name in sys.modules
    if close_body.startswith('raise'):
        retained.implementation.aclose = AsyncMock()
    asyncio.run(retained.aclose())
    assert set(sys.modules) == before


def test_direct_sync_loader_rejects_running_loop_before_import_or_factory(tmp_path):
    marker = tmp_path / 'executed'
    location = source(tmp_path, f'from pathlib import Path\nPath({str(marker)!r}).write_text("imported")\ndef create(config, services): raise AssertionError("factory executed")\n')
    before = set(sys.modules)
    async def scenario():
        with pytest.raises(ProviderError, match='provider_async_context_requires_aload_provider'):
            load_provider(location, {}, {})
    asyncio.run(scenario())
    assert not marker.exists()
    assert set(sys.modules) == before


def test_ui_launch_requires_advertised_capability(tmp_path):
    ui = source(tmp_path, 'class Adapter:\n    version=1\n    async def astart(self): self.launched = True\n    async def aclose(self): pass\ndef create(config, services): return Adapter()\n')
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', identity_factory_path=None),
                              {'UI_PROVIDER': ui}, callback_origin='https://example.test')
    with pytest.raises(RuntimeLifecycleError, match='start_failed'):
        asyncio.run(runtime.astart())
    assert runtime.ui_provider.closed
    assert not getattr(runtime.ui_provider.implementation, 'launched', False)


def test_unused_nested_default_does_not_defeat_effective_override(tmp_path):
    valid = source(tmp_path / 'valid', 'raise AssertionError()\n', '[fields.EXTRA]\ntype="int"\ndefault=7\n')
    api = source(tmp_path / 'api', 'raise AssertionError()\n', '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\ndefault="missing.py:create"\n')
    base = ConfigSchema({'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=api)})
    schema = bootstrap_schema(base, [], {'UPSTREAM_API_PROVIDER': valid}, {})
    assert schema.fields['EXTRA'].default == 7
    empty = bootstrap_schema(base, [], {'UPSTREAM_API_PROVIDER': ''}, {})
    assert 'EXTRA' not in empty.fields
    cli = bootstrap_schema(base, ['--upstream-api-provider', valid], {}, {})
    assert cli.fields['EXTRA'].default == 7
    empty_cli = bootstrap_schema(base, ['--upstream-api-provider='], {}, {})
    assert 'EXTRA' not in empty_cli.fields
    malformed = source(tmp_path / 'malformed', 'raise AssertionError()\n', '[fields.UPSTREAM_API_PROVIDER]\ntype="str"\ndefault="invalid"\n')
    base = ConfigSchema({'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=malformed)})
    assert 'EXTRA' not in bootstrap_schema(base, [], {'UPSTREAM_API_PROVIDER': ''}, {}).fields


def test_same_file_distinct_factories_are_not_a_cycle(tmp_path):
    location = source(tmp_path, 'raise AssertionError()\n')
    schema = ConfigSchema({
        'API_PROVIDER': FieldDeclaration('API_PROVIDER', 'str', default=location),
        'UI_PROVIDER': FieldDeclaration('UI_PROVIDER', 'str', default=location.replace(':create', ':create_ui')),
    })
    assert bootstrap_schema(schema, [], {}, {}) == schema


@pytest.mark.parametrize('hook', [
    'def astart(self): pass', 'async def astart(self, required): pass',
    'def aclose(self): pass', 'async def aclose(self, required): pass',
    'def authenticate(self, **kwargs): pass', 'async def authenticate(self): pass',
    'def setup_routes(self): pass', 'def setup_routes(self, registrar, services): yield None',
    'async def setup_routes(self, registrar, services): yield None',
])
def test_optional_hooks_are_validated_without_calling_them(tmp_path, hook):
    location = source(tmp_path, f'class Adapter:\n    version=1\n    {hook}\ndef create(config, services): return Adapter()\n')
    with pytest.raises(ProviderError) as error:
        load_provider(location, {}, {})
    from unittest.mock import AsyncMock
    for owner in error.value.retained_owners:
        owner.implementation.aclose = AsyncMock()
        asyncio.run(owner.aclose())


def test_stream_factory_boundary_is_checked_only_at_invocation(tmp_path):
    location = source(tmp_path, '''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version=1
    capabilities=ProviderCapabilities('llm', ('stream',))
    def __init__(self): self.calls=[]
    def astream(self, request, *, auth=None):
        self.calls.append(request)
        return request
def create(config, services): return Adapter()
''')
    owner = load_provider(location, {}, {})
    assert owner.implementation.calls == []
    view = owner.require('llm', 'stream')
    assert view is not owner.implementation and view.calls is owner.implementation.calls
    async def values():
        yield 'neutral'
    iterator = values()
    assert view.astream(iterator) is iterator
    asyncio.run(iterator.aclose())
    for value in [None, 'neutral']:
        with pytest.raises(ProviderError, match='stream_invalid'):
            view.astream(value)
    async def invalid():
        return None
    coroutine = invalid()
    with pytest.raises(ProviderError):
        view.astream(coroutine)
    assert coroutine.cr_frame is None
    owner.implementation.astream = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('private'))
    with pytest.raises(ProviderError):
        view.astream(None)
    asyncio.run(owner.aclose())
    class Generator:
        capabilities = ProviderCapabilities('llm', ('stream',))
        def astream(self, request, *, auth=None):
            yield request
    with pytest.raises(ProviderError):
        validate_capabilities(Generator())


def test_loader_disposal_is_shared_retained_and_retryable(tmp_path):
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import aload_provider
    async def scenario():
        location = source(tmp_path, 'class Adapter: version=1\ndef create(config, services): return Adapter()\n')
        owner = await aload_provider(location, {}, {})
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        owner.implementation.aclose = close
        owner.timeout = .02
        first = asyncio.create_task(owner.aclose())
        await entered.wait()
        second = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not second.done()
        with pytest.raises(ProviderError) as error:
            await first
        assert error.value.retained_owners == (owner,)
        with pytest.raises(ProviderError):
            await second
        assert owner.module_name in sys.modules and not owner.disposed
        release.set()
        await owner.aclose()
        assert owner.disposed and owner.module_name not in sys.modules
        invalid = source(tmp_path / 'invalid', 'class Adapter:\n    version=2\n    async def aclose(self): await self.services["close"]()\n    def __init__(self, services): self.services=services\ndef create(config, services): return Adapter(services)\n')
        entered.clear()
        release.clear()
        loading = asyncio.create_task(aload_provider(invalid, {}, {'close': close}))
        await entered.wait()
        loading.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await loading
        retained = cancelled.value.retained_owners[0]
        assert retained.module_name in sys.modules and not retained.disposed
        release.set()
        await retained.aclose()
        assert retained.disposed
    asyncio.run(scenario())


def test_authentication_hook_shapes_and_cancelled_disposal(tmp_path):
    from unittest.mock import AsyncMock
    async def scenario():
        from api_agent.providers.loading import aload_provider
        ui = source(tmp_path / 'ui-auth', '''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version=1
    capabilities=ProviderCapabilities('ui')
    async def authenticate(self, *, session, tui_mode, user_index, credentials): pass
def create(config, services): return Adapter()
''')
        llm = source(tmp_path / 'llm-auth', '''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version=1
    capabilities=ProviderCapabilities('llm', credential_roles=('llm-user',), llm_role='llm-user')
    async def authenticate(self, *, session, user_index, credentials): pass
def create(config, services): return Adapter()
''')
        owners = [await aload_provider(location, {}, {}) for location in [ui, llm]]
        for owner in owners:
            assert not owner.closed
            await owner.aclose()
        owner = await aload_provider(source(tmp_path / 'cancel', 'class Adapter: version=1\ndef create(config, services): return Adapter()\n'), {}, {})
        owner.implementation.aclose = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await owner.aclose()
        assert not owner.disposed and owner.module_name in sys.modules
        owner.implementation.aclose.side_effect = None
        await owner.aclose()
        assert owner.disposed
    asyncio.run(scenario())


def test_direct_provider_owners_reject_foreign_context_before_hooks(tmp_path):
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import aload_provider
    async def scenario():
        owner = await aload_provider(source(tmp_path, 'class Adapter: version=1\ndef create(config, services): return Adapter()\n'), {}, {})
        owner.implementation.astart = AsyncMock()
        owner.implementation.setup_routes = AsyncMock()
        owner.implementation.aclose = AsyncMock()
        await owner.astart()
        owner.implementation.astart.reset_mock()
        for attribute, foreign in [('_pid', -1), ('_loop', object())]:
            original = getattr(owner, attribute)
            setattr(owner, attribute, foreign)
            for action in [owner.astart, lambda: owner.setup_routes(None, None), owner.aclose]:
                with pytest.raises(ProviderError, match='execution_context'):
                    await action()
            with pytest.raises(ProviderError, match='execution_context'):
                owner.require('llm', 'complete')
            assert not owner.closed
            setattr(owner, attribute, original)
        owner.implementation.astart.assert_not_called()
        owner.implementation.setup_routes.assert_not_called()
        owner.implementation.aclose.assert_not_called()
        await owner.aclose()
        with pytest.raises(ProviderError, match='provider_closed'):
            await owner.setup_routes(None, None)
    asyncio.run(scenario())


def test_scheduled_factory_result_remains_owned_until_late_adapter_disposal(tmp_path):
    from api_agent.providers.loading import aload_provider
    async def scenario():
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        location = source(tmp_path, '''import asyncio
class Adapter:
    version=1
    def __init__(self, services): self.services=services
    async def aclose(self): self.services['cleaned'].set()
def create(config, services):
    async def create_later():
        services['entered'].set()
        try: await asyncio.Event().wait()
        except asyncio.CancelledError: await services['release'].wait()
        return Adapter(services)
    return asyncio.create_task(create_later())
''')
        loading = asyncio.create_task(aload_provider(location, {}, dict(entered=entered, release=release, cleaned=cleaned)))
        await entered.wait()
        loading.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await loading
        owner = cancelled.value.retained_owners[0]
        owner.timeout = .02
        with pytest.raises(ProviderError):
            await owner.aclose()
        assert owner.module_name in sys.modules and not cleaned.is_set()
        with pytest.raises(ProviderError):
            await owner.aclose()
        assert not cleaned.is_set()
        release.set()
        await owner.aclose()
        assert cleaned.is_set() and owner.disposed and owner.module_name not in sys.modules
    asyncio.run(scenario())


def test_scheduled_factory_future_and_failed_task_are_drained(tmp_path):
    from api_agent.providers.loading import aload_provider
    async def scenario():
        location = source(tmp_path, '''import asyncio
def create(config, services):
    future = asyncio.get_running_loop().create_future()
    services['future'] = future
    return future
''')
        services = {}
        with pytest.raises(ProviderError):
            await aload_provider(location, {}, services)
        assert services['future'].cancelled()
        location = source(tmp_path / 'failed-task', '''import asyncio
def create(config, services):
    async def create_later(): raise RuntimeError('private')
    return asyncio.create_task(create_later())
''')
        before = set(sys.modules)
        with pytest.raises(ProviderError):
            await aload_provider(location, {}, {})
        assert set(sys.modules) == before
    asyncio.run(scenario())


def test_foreign_loop_factory_future_cannot_migrate(tmp_path):
    from api_agent.providers.loading import aload_provider
    location = source(tmp_path, "def create(config, services): return services['future']\n")
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    with pytest.raises(ProviderError) as error:
        load_provider(location, {}, {'future': future})
    owner = error.value.retained_owners[0]
    assert owner.module_name in sys.modules
    async def wrong_loop():
        with pytest.raises(ProviderError, match='execution_context'):
            await owner.aclose()
        assert not future.cancelled()
    asyncio.run(wrong_loop())
    loop.run_until_complete(owner.aclose())
    assert future.cancelled() and owner.disposed
    loop.close()
    foreign = asyncio.new_event_loop()
    future = foreign.create_future()
    async def loading():
        with pytest.raises(ProviderError) as error:
            await aload_provider(location, {}, {'future': future})
        return error.value.retained_owners[0]
    retained = asyncio.run(loading())
    foreign.run_until_complete(retained.aclose())
    foreign.close()


def test_stream_carrier_requires_callable_iterator_protocol(tmp_path):
    location = source(tmp_path, '''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version=1
    capabilities=ProviderCapabilities('llm', ('stream',))
    def astream(self, request, *, auth=None): return request
def create(config, services): return Adapter()
''')
    owner = load_provider(location, {}, {})
    for returned in [SimpleNamespace(__aiter__=None, __anext__=None),
                     SimpleNamespace(__aiter__=lambda: None, __anext__=None)]:
        with pytest.raises(ProviderError, match='stream_invalid'):
            owner.require('llm', 'stream').astream(returned)
    asyncio.run(owner.aclose())


def test_returned_awaitable_disposes_failed_late_result_and_preserves_cancellation(tmp_path):
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import _ReturnedAwaitable, LoadedProvider
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        adapter = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError('private')))
        async def later():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return adapter
        returned = asyncio.create_task(later())
        await entered.wait()
        returned.cancel()
        await asyncio.sleep(0)
        wrapper = _ReturnedAwaitable(returned, '_synthetic_no_module')
        closing = asyncio.create_task(wrapper.aclose())
        await asyncio.sleep(0)
        assert returned.cancelling() == 1
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not returned.done()
        release.set()
        with pytest.raises(ProviderError):
            await wrapper.aclose()
        assert wrapper._result_owner is not None and not wrapper._result_owner.disposed
        adapter.aclose.side_effect = None
        await wrapper.aclose()
        assert wrapper._result_owner.disposed
        foreign = asyncio.new_event_loop()
        future = foreign.create_future()
        wrapper = _ReturnedAwaitable(future, '_synthetic_no_module')
        with pytest.raises(ProviderError, match='execution_context'):
            await wrapper.aclose()
        assert not future.cancelled()
        future.cancel()
        foreign.close()
    asyncio.run(scenario())


def test_opaque_custom_awaitable_keeps_hidden_task_until_exact_disposal(tmp_path):
    from api_agent.providers.loading import aload_provider
    async def scenario():
        entered, allocate, allocated = asyncio.Event(), asyncio.Event(), asyncio.Event()
        services = dict(entered=entered, allocate=allocate, allocated=allocated, closes=[])
        location = source(tmp_path, '''import asyncio
class Adapter:
    version=1
    def __init__(self, services): self.services=services
    async def aclose(self): self.services['closes'].append('disposed')
class Deferred:
    def __init__(self, services):
        async def build():
            services['entered'].set()
            await services['allocate'].wait()
            services['allocated'].set()
            return Adapter(services)
        self.task = asyncio.create_task(build())
    def __await__(self): return asyncio.shield(self.task).__await__()
def create(config, services):
    result = Deferred(services)
    services['returned'] = result
    return result
''')
        loading = asyncio.create_task(aload_provider(location, {}, services))
        await entered.wait()
        loading.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await loading
        owner = cancelled.value.retained_owners[0]
        owner.timeout = .02
        with pytest.raises(ProviderError):
            await owner.aclose()
        assert owner.module_name in sys.modules
        assert not services['returned'].task.done() and not allocated.is_set()
        with pytest.raises(ProviderError):
            await owner.aclose()
        assert services['closes'] == []
        allocate.set()
        await owner.aclose()
        assert allocated.is_set() and services['closes'] == ['disposed']
        assert owner.disposed and owner.module_name not in sys.modules
        await owner.aclose()
        assert services['closes'] == ['disposed']
    asyncio.run(scenario())
