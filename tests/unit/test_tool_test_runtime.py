"""Worker tooling consumes exact captured cases and retains real lifecycle duties."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from threading import Event
import time
from types import MappingProxyType, SimpleNamespace

import pytest

from api_agent.api.application import RuntimeLifecycleError
from api_agent.config.declarations import ConfigSchema
from api_agent.config.resolve import resolve_config
from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.testing import provider_setup
from api_agent.testing import tool_runtime as worker
from api_agent.tooling.models import ToolSelectionError
from api_agent.tooling.selection import compose_tools


CASE = 'fixture::tests/integration/test_one.py::test_case'
OTHER_CASE = 'fixture::tests/integration/test_two.py::test_case'
NODE = CASE.split('::', 1)[1]
SOURCE = '''from pathlib import Path
class Owner:
    version=1
    def __init__(self, config, services):
        self.config, self.root = config, Path(services['owner_root'])
    def record(self, event):
        with (self.root/'events').open('a') as stream: stream.write(event+'\\n')
    async def astart(self):
        self.record('start:'+self.config['LABEL'])
        if (self.root/'fail-start').exists(): raise ValueError('startup failed')
    async def describe(self): return 'synthetic description'
    async def validate(self, prepared): pass
    async def invoke(self, prepared): return self.config['LABEL']
    async def aclose(self):
        self.record('close:'+self.config['LABEL'])
        if (self.root/'blocked').exists(): raise ValueError('cleanup pending')
def create_tool(config, services): return Owner(config,services)
'''


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.fixture
def packet(tmp_path, monkeypatch):
    root = tmp_path / 'fixture'
    root.mkdir()
    write(root, 'provider/factory.py', SOURCE)
    write(root, 'native.py', 'def read(inputs,context): return {}\n')
    write(root, 'empty.json', '{"type":"object","properties":{},"additionalProperties":false}')
    write(root, 'implementation.toml', 'version=1\ninterface="verified-read@1"\nfactory="provider/factory.py:create_tool"\n'
          '[inputs.LABEL]\ntype="str"\ndefault="one"\n[inputs.ENDPOINT]\ntype="str"\ndefault="external"\n')
    for name in ('one', 'two'):
        write(root, name + '.toml', f'version=1\nid="fixture/{name}"\ninterface="verified-read@1"\nimplementation="implementation.toml"\n'
              f'[bindings]\nLABEL="{name.upper()}_LABEL"\nENDPOINT="{name.upper()}_ENDPOINT"\n[defaults]\nLABEL="{name}"\n')
        write(root, name + '-node.md.html', f'# Node v2: read\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n'
              '| read | deterministic | [source](native.py) |\n\n## Functions\n\n| ID | Registry | Input | Output |\n'
              '| --- | --- | --- | --- |\n| read | read | [input](empty.json) | [output](empty.json) |\n\n## Tools\n\n'
              f'| Port | Instance |\n| --- | --- |\n| reader | [instance]({name}.toml) |\n')
        write(root, name + '.md.html', f'# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| {name} |\n\n'
              f'## Flow\n\n1.  [node read]({name}-node.md.html)\n')
    catalog = write(root, 'catalog.toml', f'version=1\n[graphs.one]\nroot="base"\nentry="one.md.html"\n'
        f'[graphs.two]\nroot="base"\nentry="two.md.html"\n[test_cases]\n"{CASE}"=["one"]\n"{OTHER_CASE}"=["two"]\n')
    context = LaunchContext(root, root, root, 'fixture', 'fixture', root / 'config.toml',
        MappingProxyType({}), MappingProxyType({}), workflow_entry=root / 'one.md.html', tool_catalog_path=catalog)
    plan = compose_tools(context, ConfigSchema({}), selection=(CASE, OTHER_CASE), environ={})
    plan = plan.bind_values(resolve_config(plan.schema, environ={}))
    directory = tmp_path / 'worker'
    directory.mkdir(mode=0o700)
    path = provider_setup.write_launch_snapshot(directory, context, plan.config.values,
        password_manager_registry=SimpleNamespace(definitions=()), tool_plan=plan, tool_cases={NODE: CASE})
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    loop = asyncio.new_event_loop()
    result = SimpleNamespace(root=root, context=context, plan=plan, directory=directory,
        path=path, data=provider_setup._read_snapshot(), loop=loop)
    yield result
    if not loop.is_closed():
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def owner(packet, **kwargs):
    return worker.PreparedTestTools(packet.data, NODE, packet.loop, engine_environment={}, **kwargs)


def test_packet_roundtrip_narrows_actual_parent_union_without_reparsing(packet):
    assert stat.S_IMODE((packet.directory / 'tool-plan.json').stat().st_mode) == 0o600
    assert len(packet.plan.tools) == 2
    write(packet.root, 'one.toml', 'changed after launch')
    prepared = owner(packet)
    assert [tool.id for tool in prepared.plan.tools] == ['fixture/one']
    assert [definition.graph_id for definition in prepared.plan.definitions] == ['one']
    assert packet.root / 'two.toml' not in prepared.plan.captured_assets
    assert prepared.definition_for_case('one') is prepared.definition
    assert prepared.sources['ONE_LABEL'] == {'source': 'shared_default', 'explicit': False}


def test_unknown_case_and_foreign_definition_reject_before_effects(packet):
    with pytest.raises(ContractError, match='no launch-admitted'):
        worker.PreparedTestTools(packet.data, 'unknown', packet.loop)
    forged = {**packet.data, 'tool_cases': {NODE: 'fixture::forged'}}
    with pytest.raises(ToolSelectionError, match='outside'):
        worker.PreparedTestTools(forged, NODE, packet.loop)
    prepared = owner(packet)
    with pytest.raises(ContractError, match='outside'):
        prepared.definition_for_case('two')

    async def scenario():
        with pytest.raises(ContractError, match='outside'):
            async with prepared.open(packet.plan.definitions[1]):
                raise AssertionError('Foreign graph started')
    prepared.run(scenario())
    assert not (packet.root / 'events').exists()


def test_real_runtime_and_real_empty_infrastructure_need_no_engine(packet, monkeypatch):
    from api_agent.runtime.prerequisites import PrerequisiteManager
    monkeypatch.setattr(PrerequisiteManager, '_command', lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError('Unexpected engine command')))
    prepared = owner(packet)

    async def scenario():
        async with prepared.open(prepared.definition) as operation:
            assert operation.endpoint_for('fixture/one') == 'external'
            with pytest.raises(ContractError, match='no ready endpoint'):
                operation.endpoint_for('fixture/two')
            view = operation.tools.for_port(scope_kind='node', scope='read', port='reader')
            assert await view.invoke(None) == 'one'
            with pytest.raises(ContractError, match='already owned'):
                async with prepared.open(prepared.definition):
                    pass
        assert view.closed
        assert operation.receipt == {'tool_count': 1, 'service_count': 0, 'closed_tools': 1,
                                     'remaining_owners': 0, 'released': True}
        await prepared.aclose()
    prepared.run(scenario())
    assert (packet.root / 'events').read_text().splitlines() == ['start:one', 'close:one']


class Manager:
    def __init__(self, plan, scope, directory, *, events, **kwargs):
        self.conditional = {item.compose_name: item for item in plan.conditional}
        self.timeout, self.now, self.lease = 30, time.monotonic, None
        self.events, self.fail_release = events, False
        self.acquire_failure = False

    def acquire(self, *, _deadline):
        assert _deadline > self.now()
        self.events.append('acquire')
        self.lease = object()
        if self.acquire_failure:
            raise ValueError('acquisition failed')
        return self.lease

    def release(self, *, retain):
        assert retain is False
        self.events.append('release')
        if self.fail_release:
            raise ValueError('release pending')
        self.lease = None


def manager_factory(events, **changes):
    managers = []
    def factory(*args, **kwargs):
        manager = Manager(*args, events=events, **kwargs)
        for key, value in changes.items():
            setattr(manager, key, value)
        managers.append(manager)
        return manager
    return factory, managers


def test_pending_runtime_retains_parent_lease_until_retry_on_creating_loop(packet):
    events = []
    factory, managers = manager_factory(events)
    prepared = owner(packet, manager_factory=factory)
    write(packet.root, 'blocked', '')

    async def scenario():
        with pytest.raises(CleanupPendingError):
            async with prepared.open(prepared.definition) as operation:
                assert operation.runtime._loop is packet.loop
        assert events == ['acquire']
        assert managers[0].lease is not None
        assert prepared.pending_cleanup is not None
        assert operation.receipt['released'] is False
        with pytest.raises(ContractError, match='awaiting cleanup'):
            async with prepared.open(prepared.definition):
                pass
        (packet.root / 'blocked').unlink()
        await prepared.aclose()
        assert prepared.pending_cleanup is None
        assert operation.receipt['released'] is True
        assert events == ['acquire', 'release']
    prepared.run(scenario())
    assert (packet.root / 'events').read_text().splitlines() == ['start:one', 'close:one', 'close:one']


def test_primary_workload_failure_keeps_its_cleanup_receipt(packet):
    events = []
    factory, managers = manager_factory(events)
    prepared = owner(packet, manager_factory=factory)
    write(packet.root, 'blocked', '')
    failure = ValueError('workload failed')

    async def scenario():
        with pytest.raises(ValueError) as caught:
            async with prepared.open(prepared.definition):
                raise failure
        assert caught.value is failure
        assert failure.cleanup_pending is prepared.pending_cleanup
        assert events == ['acquire']
        (packet.root / 'blocked').unlink()
        await prepared.aclose()
        assert events == ['acquire', 'release']
    prepared.run(scenario())


def test_startup_failure_closes_runtime_before_releasing_parent(packet):
    events = []
    factory, managers = manager_factory(events)
    prepared = owner(packet, manager_factory=factory)
    write(packet.root, 'fail-start', '')

    async def scenario():
        with pytest.raises(RuntimeLifecycleError) as caught:
            async with prepared.open(prepared.definition):
                raise AssertionError('Failed startup yielded')
        assert str(caught.value.primary_failure) == 'startup failed'
        assert (packet.root / 'events').read_text().splitlines() == ['start:one', 'close:one']
        assert events == ['acquire', 'release']
        assert prepared.pending_cleanup is None
    prepared.run(scenario())


def test_failed_parent_release_remains_retryable_without_reclosing_tools(packet):
    events = []
    factory, managers = manager_factory(events, fail_release=True)
    prepared = owner(packet, manager_factory=factory)

    async def scenario():
        with pytest.raises(CleanupPendingError):
            async with prepared.open(prepared.definition) as operation:
                pass
        assert events == ['acquire', 'release']
        assert prepared.pending_cleanup is not None
        managers[0].fail_release = False
        await prepared.aclose()
        assert events == ['acquire', 'release', 'release']
        assert operation.receipt['released'] is True
    prepared.run(scenario())
    assert (packet.root / 'events').read_text().splitlines() == ['start:one', 'close:one']


@pytest.mark.parametrize('descriptor', [{}, {'file': '../tool-plan.json', 'sha256': 'a' * 64},
    {'file': 'tool-plan.json', 'sha256': 'A' * 64}, {'file': 'tool-plan.json', 'sha256': None},
    {'file': 'tool-plan.json', 'sha256': 'a' * 64, 'extra': True}])
def test_packet_descriptor_rejects_before_opening_any_file(packet, monkeypatch, descriptor):
    def unexpected_open(*args, **kwargs):
        raise AssertionError('Invalid descriptor reached filesystem')
    monkeypatch.setattr(worker.os, 'open', unexpected_open)
    with pytest.raises(ContractError, match='Invalid selected tool context'):
        worker.read_tool_packet(packet.path, descriptor)


def test_packet_writer_rejects_oversize_before_creating_file(packet, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, 'MAX_PACKET_BYTES', 1)
    directory = tmp_path / 'oversize'
    directory.mkdir()
    with pytest.raises(ContractError, match='worker budget'):
        worker.write_tool_packet(directory, packet.plan)
    assert not (directory / 'tool-plan.json').exists()


def test_packet_writer_never_replaces_existing_symlink(packet, tmp_path):
    target = write(tmp_path, 'target', 'unchanged')
    directory = tmp_path / 'symlink-writer'
    directory.mkdir()
    (directory / 'tool-plan.json').symlink_to(target)
    with pytest.raises(FileExistsError):
        worker.write_tool_packet(directory, packet.plan)
    assert target.read_text() == 'unchanged'


@pytest.mark.parametrize('kind', ['mode', 'symlink', 'fifo', 'directory', 'hash'])
def test_protected_packet_rejects_invalid_storage_without_factory_effects(packet, kind):
    path = packet.directory / 'tool-plan.json'
    descriptor = dict(packet.data['tool_plan'])
    if kind == 'mode':
        path.chmod(0o644)
    elif kind == 'hash':
        descriptor['sha256'] = '0' * 64
    else:
        target = path.with_suffix('.original')
        path.rename(target)
        if kind == 'symlink':
            path.symlink_to(target)
        elif kind == 'fifo':
            os.mkfifo(path, 0o600)
        else:
            path.mkdir(mode=0o700)
    with pytest.raises((ContractError, OSError)):
        worker.read_tool_packet(packet.path, descriptor)
    assert not (packet.root / 'events').exists()


def test_packet_with_foreign_owner_is_rejected_independently_of_mode(packet, monkeypatch):
    monkeypatch.setattr(worker.os, 'getuid', lambda: (packet.directory / 'tool-plan.json').stat().st_uid + 1)
    with pytest.raises(ContractError, match='Invalid selected tool context'):
        worker.read_tool_packet(packet.path, packet.data['tool_plan'])


def test_packet_pre_read_size_limit_is_independent_of_hash(packet, monkeypatch):
    size = (packet.directory / 'tool-plan.json').stat().st_size
    monkeypatch.setattr(worker, 'MAX_PACKET_BYTES', size - 1)
    with pytest.raises(ContractError, match='Invalid selected tool context'):
        worker.read_tool_packet(packet.path, packet.data['tool_plan'])


def test_packet_growth_read_bound_is_independent_of_hash_and_final_stat(packet, monkeypatch):
    limit = (packet.directory / 'tool-plan.json').stat().st_size - 1
    original = os.fstat
    def smaller_size(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid,
                               st_size=limit, st_mtime_ns=info.st_mtime_ns)
    monkeypatch.setattr(worker, 'MAX_PACKET_BYTES', limit)
    monkeypatch.setattr(worker, 'os', SimpleNamespace(**(vars(worker.os) | {'fstat': smaller_size})))
    with pytest.raises(ContractError, match='changed after launch'):
        worker.read_tool_packet(packet.path, packet.data['tool_plan'])


def test_packet_stat_change_is_rejected_even_when_hash_still_matches(packet, monkeypatch):
    original, observations = os.fstat, []
    def changing_stat(fd):
        info = original(fd)
        observations.append(info)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid,
            st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + len(observations))
    monkeypatch.setattr(worker, 'os', SimpleNamespace(**(vars(worker.os) | {'fstat': changing_stat})))
    with pytest.raises(ContractError, match='changed after launch'):
        worker.read_tool_packet(packet.path, packet.data['tool_plan'])


def replace_packet(packet, change):
    path = packet.directory / 'tool-plan.json'
    data = json.loads(path.read_text())
    change(data)
    payload = json.dumps(data).encode()
    path.write_bytes(payload)
    return {'file': 'tool-plan.json', 'sha256': sha256(payload).hexdigest()}


@pytest.mark.parametrize('change', [lambda data: data.update(version=True),
    lambda data: data.update(extra=True), lambda data: data.update(sources=[]),
    lambda data: data.update(sources={'ONE_LABEL': {'source': 'default', 'explicit': 1}}),
    lambda data: data.update(sources={'ONE_LABEL': {'source': 1, 'explicit': False}}),
    lambda data: data.update(sources={'ONE_LABEL': {'source': 'default'}})])
def test_packet_rejects_invalid_outer_contract_and_provenance_before_model_decode(packet, monkeypatch, change):
    from api_agent.tooling.models import ToolPlan
    descriptor = replace_packet(packet, change)
    def unexpected_decode(value):
        raise AssertionError('Invalid packet reached plan decode')
    monkeypatch.setattr(ToolPlan, 'from_wire', unexpected_decode)
    with pytest.raises(ContractError):
        worker.read_tool_packet(packet.path, descriptor)


def test_helper_with_multi_graph_case_requires_explicit_graph_choice(packet):
    plan = replace(packet.plan, case_definitions={CASE: tuple(item.definition_id for item in packet.plan.definitions)})
    (packet.directory / 'tool-plan.json').unlink()
    packet.data['tool_plan'] = worker.write_tool_packet(packet.directory, plan, context=packet.context)
    prepared = owner(packet)
    with pytest.raises(ContractError, match='choose one'):
        prepared.definition
    assert prepared.definition_for_case('one').graph_id == 'one'


def test_real_workflow_fixture_retains_pending_cleanup_in_existing_inspection_protocol(packet, monkeypatch):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(packet.directory / 'events.jsonl'))
    generator = provider_setup.workflow_tools.__wrapped__(SimpleNamespace(node=SimpleNamespace(nodeid=NODE)), packet.loop)
    prepared = next(generator)
    write(packet.root, 'blocked', '')

    async def scenario():
        with pytest.raises(CleanupPendingError):
            async with prepared.open(prepared.definition):
                pass
    prepared.run(scenario())
    with pytest.raises(CleanupPendingError):
        next(generator)
    retained = provider_setup._inspection_providers[-1]
    assert retained.loop is packet.loop and not packet.loop.is_closed()
    assert retained._owners == [prepared]
    (packet.root / 'blocked').unlink()
    try:
        retained.close()
        assert packet.loop.is_closed()
        assert prepared.pending_cleanup is None
    finally:
        provider_setup._inspection_providers.remove(retained)


def managed_packet(packet):
    from api_agent.config.resolve import ResolvedValue
    from api_agent.runtime.infrastructure_contract import load_infrastructure_contributions
    declaration = write(packet.root, 'infrastructure.toml', 'version=1\nowner="fixture"\n'
        '[[services]]\nid="backing"\ncompose="backing.json"\nreadiness={kind="health"}\n'
        '[[services]]\nid="api"\ncompose="api.json"\ndependencies=["fixture/backing"]\n'
        'readiness={kind="health"}\nendpoints=[{name="http",host="http://127.0.0.1:58140",in_project="http://self:8090"}]\n')
    image = 'example/fixture@sha256:' + 'a' * 64
    write(packet.root, 'backing.json', json.dumps({'image': image}))
    write(packet.root, 'api.json', json.dumps({'image': image, 'ports': ['127.0.0.1:58140:8090']}))
    services = load_infrastructure_contributions(SimpleNamespace(pack_root=packet.root,
        pack_id='fixture', infrastructure_paths=(declaration,)))
    local = {'slot': 'ENDPOINT', 'output': 'fixture/api/http', 'path': '/graphql', 'declaration': declaration}
    one = replace(packet.plan.tools[0], pending={'ENDPOINT': local},
        infrastructure={'owner': 'fixture', 'owner_root': packet.root, 'declarations': (declaration,)})
    sources = dict(packet.plan.captured_assets)
    for service in services:
        for asset in (service.declaration_source, service.compose_source, *service.assets):
            sources[packet.root / asset.relative_path] = asset.body
    values = {**packet.plan.config.values, 'ONE_ENDPOINT': ''}
    fields = {**packet.plan.config.fields, 'ONE_ENDPOINT': ResolvedValue('', 'shared_default', False)}
    config = replace(packet.plan.config, values=values, fields=fields)
    plan = replace(packet.plan, tools=(one, packet.plan.tools[1]),
                   infrastructure_contributions=services, captured_assets=sources).bind_values(config)
    (packet.directory / 'tool-plan.json').unlink()
    packet.data['tool_plan'] = worker.write_tool_packet(packet.directory, plan, context=packet.context)
    from api_agent.runtime.inventory import inventory_authority
    packet.data['inventory_authority'] = inventory_authority(packet.context, plan)
    packet.data['config'] = values
    packet.path.write_text(json.dumps(packet.data))
    return plan


def test_managed_ready_outputs_use_real_infrastructure_session_and_verified_transport_fallback(packet, monkeypatch):
    from api_agent.runtime import reporting_engine
    managed_packet(packet)
    events, transports = [], []
    def verify(environment):
        transports.append(environment)
        return 'verified-engine', 'unix:///run/synthetic/engine.sock'
    monkeypatch.setattr(reporting_engine, 'verified_engine_binding', verify)
    factory, managers = manager_factory(events)
    prepared = owner(packet, manager_factory=factory)

    async def scenario():
        async with prepared.open(prepared.definition) as operation:
            assert transports == [{}]
            assert [service.id for service in managers[0].conditional.values()] == ['fixture/backing', 'fixture/api']
            assert operation.endpoint_for('fixture/one') == 'http://127.0.0.1:58140/graphql'
            assert operation.endpoints['fixture/api'][0].host == 'http://127.0.0.1:58140'
            assert not operation.plan.tools[0].pending
            assert events == ['acquire']
            assert (packet.root / 'events').read_text().splitlines() == ['start:one']
        assert events == ['acquire', 'release']
        assert operation.receipt['service_count'] == 2
        assert operation.receipt['released'] is True
    prepared.run(scenario())


def test_cancelled_cleanup_waiter_drains_release_and_does_not_invent_pending_owners(packet):
    entered, release = Event(), Event()
    events = []
    managers = []
    class SlowRelease(Manager):
        def release(self, *, retain):
            entered.set()
            assert release.wait(5), 'Release was not unblocked'
            super().release(retain=retain)
    def factory(*args, **kwargs):
        manager = SlowRelease(*args, events=events, **kwargs)
        managers.append(manager)
        return manager
    prepared = owner(packet, manager_factory=factory)
    operations = []

    async def workload():
        async with prepared.open(prepared.definition) as operation:
            operations.append(operation)

    async def scenario():
        task = asyncio.create_task(workload())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ['acquire', 'release']
        assert prepared.pending_cleanup is None
        assert operations[0].receipt['released'] is True
        assert managers[0].lease is None
        await prepared.aclose()
    prepared.run(scenario())


def test_failed_startup_with_failed_cleanup_retains_primary_and_parent(packet):
    events = []
    factory, managers = manager_factory(events)
    prepared = owner(packet, manager_factory=factory)
    write(packet.root, 'fail-start', '')
    write(packet.root, 'blocked', '')

    async def scenario():
        with pytest.raises(RuntimeLifecycleError) as caught:
            async with prepared.open(prepared.definition):
                raise AssertionError('Failed startup yielded')
        assert str(caught.value.primary_failure) == 'startup failed'
        assert caught.value.cleanup_pending is prepared.pending_cleanup
        assert managers[0].lease is not None
        assert events == ['acquire']
        (packet.root / 'blocked').unlink()
        await prepared.aclose()
        assert events == ['acquire', 'release']
        assert prepared.pending_cleanup is None
    prepared.run(scenario())


def test_acquisition_failure_after_allocation_still_releases_its_lease(packet):
    events = []
    factory, managers = manager_factory(events, acquire_failure=True)
    prepared = owner(packet, manager_factory=factory)

    async def scenario():
        with pytest.raises(ValueError, match='acquisition failed'):
            async with prepared.open(prepared.definition):
                raise AssertionError('Failed acquisition yielded')
        assert managers[0].lease is None
        assert events == ['acquire', 'release']
        assert prepared.pending_cleanup is None
    prepared.run(scenario())
    assert not (packet.root / 'events').exists()


def test_normal_worker_failure_keeps_partial_tool_evidence_as_allure_attachment(packet, tmp_path, monkeypatch):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from api_agent.testing.recording import RunIdentity, RunRecorder
    from api_agent.workflows.evidence import attachment
    from api_agent.tooling import services
    prepared = []
    original_prepare = services.prepare_tool_plan
    def prepare(plan):
        assert prepared == ['dependencies ready']
        prepared.append('dependencies admitted')
        return original_prepare(plan)
    monkeypatch.setattr(services, 'prepare_tool_plan', prepare)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join((str(Path(worker.__file__).resolve().parents[2]),
                                                   os.environ.get('PYTHONPATH', ''))))
    write(packet.root, 'tests/integration/test_one.py', '''import pytest
from api_agent.workflows.evidence import publish
from api_agent.workflows.tool_evidence import tool_evidence
pytestmark=pytest.mark.feature('Worker tool lifecycle')
def test_case(workflow_tools):
    journal={}
    async def scenario():
        operation=None
        try:
            async with workflow_tools.open(workflow_tools.definition) as operation:
                result=await operation.tools.for_port(scope_kind='node',scope='read',port='reader').invoke(None)
                journal['calls']=[{'tool':'reader','invocation':'worker-call','status':'success','result':result}]
                raise ValueError('Controlled failure after a completed tool')
        finally:
            receipt=operation.receipt
            publish(tool_evidence(journal,graph_id='one',instances={'reader':'fixture/one'},ownership={
                'owners_started':receipt['tool_count'],'owners_remaining':receipt['remaining_owners']}))
    workflow_tools.run(scenario())
''')
    context = replace(packet.context, tests={'integration': (packet.root / 'tests/integration',)})
    tests = collect_tests(context, 'integration')
    assert [test.id for test in tests] == [CASE]
    plan = packet.plan.bind_values(resolve_config(packet.plan.schema, environ={},
                                  overrides={'ONE_LABEL': 'private-output-value'}))
    events = []
    result = run_integration_tests(context, tests, accepted=True, config={
        **plan.config.values, 'SKIP_ALLURE_REPORTING': True}, tool_plan=plan, on_event=events.append,
        before_run=lambda: prepared.append('dependencies ready'))
    assert prepared == ['dependencies ready', 'dependencies admitted']
    assert result.exit_code == 1
    assert [test.status for test in result.tests] == ['failed']
    evidence, = [event for event in events if event.kind == 'workflow_evidence']
    report = json.loads(evidence.text)
    assert report['calls'][0]['result_received'] is True
    assert report['completed'] is False
    assert report['ownership'] == {'owners_started': 1, 'owners_remaining': 0}
    assert 'private-output-value' not in evidence.text
    boundary = EmissionBoundary(ContentPolicy(False))
    frozen = attachment(evidence, boundary)
    recorder = RunRecorder(tmp_path / 'allure', RunIdentity.create('fixture', 'integration'),
                           evidence_boundary=boundary)
    filename = recorder.record_test(result.tests[0], attachments=(frozen,))
    recorded = json.loads((recorder.directory / filename).read_text())
    assert recorded['status'] == 'failed'
    assert len(recorded['attachments']) == 1
    content = json.loads((recorder.directory / recorded['attachments'][0]['source']).read_text())
    assert content == report
