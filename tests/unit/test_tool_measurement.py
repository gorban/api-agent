"""Real locking inodes isolate measurements without serializing tool leases."""
import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'tests/integration/fixtures/tool_instances/measurement.py'


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location('fixture_measurement', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canonical_authorities_deduplicate_routes_and_sort(helper):
    assert helper.authorities(['https://EXAMPLE.test./graphql', 'https://example.test:443/hello',
        'http://127.0.0.1:80/graphql', 'http://[0:0:0:0:0:0:0:1]/hello', 'http://[::1]:80/graphql']) == (
            ('http', '127.0.0.1', 80), ('http', '::1', 80), ('https', 'example.test', 443))


@pytest.mark.parametrize('endpoints', [[], [None], ['ftp://example.test'], ['http://user:secret@example.test'],
    ['http://'], ['http://example.test:bad'], ['http://example.test:65536'], ['http://example.test:0'],
    ['http://.'], ['http://example.test/ bad'], ['http://[bad']])
def test_invalid_measurement_targets_fail_before_locking(helper, endpoints):
    with pytest.raises(ValueError, match='measurement'):
        helper.authorities(endpoints)


@pytest.mark.asyncio
async def test_private_persistent_inode_and_default_namespace(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(helper, 'cache_path', lambda: tmp_path / 'history.json')
    async with helper.measure_targets(['http://127.0.0.1/graphql']):
        path, = (tmp_path / 'tool-measurement-locks').iterdir()
        inode = path.stat().st_ino
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.name.endswith('.lock') and '127.0.0.1' not in path.name
    async with helper.measure_targets(['http://127.0.0.1/hello']):
        assert path.stat().st_ino == inode
    assert path.exists()


@pytest.mark.parametrize('kind', ['directory-public', 'directory-owner', 'directory-link',
    'file-public', 'file-owner', 'file-link', 'file-fifo', 'file-hardlink'])
def test_lock_paths_reject_unowned_public_or_redirected_inodes(helper, tmp_path, monkeypatch, kind):
    directory = tmp_path / 'locks'
    authority = ('http', '127.0.0.1', 80)
    descriptor = helper._open(directory, authority)
    os.close(descriptor)
    path, = directory.iterdir()
    if kind == 'directory-public':
        directory.chmod(0o755)
    elif kind == 'directory-link':
        original = tmp_path / 'original'; directory.rename(original); directory.symlink_to(original)
    elif kind == 'file-public':
        path.chmod(0o644)
    elif kind == 'file-link':
        original = tmp_path / 'original'; path.rename(original); path.symlink_to(original)
    elif kind == 'file-fifo':
        path.unlink(); os.mkfifo(path, 0o600)
    elif kind == 'file-hardlink':
        os.link(path, tmp_path / 'alias')
    else:
        real = os.fstat
        def ownership(fd):
            info = real(fd)
            mismatch = (kind == 'directory-owner' and os.path.isdir('/proc/self/fd/' + str(fd))) or (
                kind == 'file-owner' and not os.path.isdir('/proc/self/fd/' + str(fd)))
            return SimpleNamespace(st_uid=info.st_uid + 1 if mismatch else info.st_uid,
                st_mode=info.st_mode, st_nlink=info.st_nlink)
        monkeypatch.setattr(helper.os, 'fstat', ownership)
    with pytest.raises((ValueError, OSError)):
        helper._open(directory, authority)


@pytest.mark.asyncio
async def test_same_backend_waits_while_unrelated_backend_stays_parallel(helper, tmp_path, monkeypatch):
    blocked = asyncio.Event()
    real_flock = helper.fcntl.flock
    def flock(fd, flags):
        try:
            return real_flock(fd, flags)
        except BlockingIOError:
            blocked.set()
            raise
    monkeypatch.setattr(helper.fcntl, 'flock', flock)
    entered = asyncio.Event()
    async def waiter():
        async with helper.measure_targets(['http://example.test/hello'], directory=tmp_path):
            entered.set()
    async with helper.measure_targets(['http://example.test/graphql'], directory=tmp_path):
        task = asyncio.create_task(waiter())
        await asyncio.wait_for(blocked.wait(), 1)
        assert not entered.is_set()
        async with helper.measure_targets(['http://different.test/graphql'], directory=tmp_path, timeout=.5):
            assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['cancel', 'deadline'])
async def test_partial_acquisition_releases_first_lock_on_cancel_or_deadline(helper, tmp_path, monkeypatch, finish):
    blocked = asyncio.Event()
    real_flock = helper.fcntl.flock
    def flock(fd, flags):
        try:
            return real_flock(fd, flags)
        except BlockingIOError:
            blocked.set()
            raise
    monkeypatch.setattr(helper.fcntl, 'flock', flock)
    async def waiter():
        async with helper.measure_targets(['http://b.test', 'http://a.test'], directory=tmp_path,
                timeout=.1 if finish == 'deadline' else 2):
            pytest.fail('Blocked target cannot enter')
    async with helper.measure_targets(['http://b.test'], directory=tmp_path):
        task = asyncio.create_task(waiter())
        await asyncio.wait_for(blocked.wait(), 1)
        if finish == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await task
        async with helper.measure_targets(['http://a.test'], directory=tmp_path, timeout=.5):
            pass


@pytest.mark.asyncio
async def test_body_failure_releases_all_deduplicated_sorted_locks(helper, tmp_path, monkeypatch):
    acquired = []
    original = helper._open
    def opened(directory, authority):
        acquired.append(authority)
        return original(directory, authority)
    monkeypatch.setattr(helper, '_open', opened)
    with pytest.raises(RuntimeError, match='Synthetic'):
        async with helper.measure_targets(['http://b.test', 'http://a.test/x', 'http://a.test/y'], directory=tmp_path):
            raise RuntimeError('Synthetic body failure')
    assert acquired == [('http', 'a.test', 80), ('http', 'b.test', 80)]
    async with helper.measure_targets(['http://a.test', 'http://b.test'], directory=tmp_path, timeout=.5):
        pass


def test_fresh_process_contends_and_process_death_releases_lock(helper, tmp_path):
    script = '''
import asyncio, importlib.util, sys
spec = importlib.util.spec_from_file_location('measurement', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
async def run():
    async with m.measure_targets(['http://example.test'], directory=sys.argv[2]):
        print('LOCKED', flush=True)
        await asyncio.Event().wait()
asyncio.run(run())
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(SOURCE), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5), 'Fresh process did not report readiness'
        assert process.stdout.readline().strip() == 'LOCKED'
        async def blocked():
            with pytest.raises(TimeoutError):
                async with helper.measure_targets(['http://example.test'], directory=tmp_path, timeout=.1):
                    pytest.fail('Cross-process owner must exclude this measurement')
        asyncio.run(blocked())
        process.kill(); process.wait(timeout=5)
        async def recovered():
            async with helper.measure_targets(['http://example.test'], directory=tmp_path, timeout=.5):
                pass
        asyncio.run(recovered())
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()


@pytest.mark.asyncio
async def test_parent_deadline_clips_wait_and_releases_descriptor(helper, tmp_path):
    from api_agent.providers.progress import component_timeout
    async with helper.measure_targets(['http://example.test'], directory=tmp_path):
        with pytest.raises(TimeoutError):
            async with component_timeout('Synthetic short parent', .05):
                async with helper.measure_targets(['http://example.test'], directory=tmp_path, timeout=2):
                    pytest.fail('Child cannot extend its parent deadline')
    async with helper.measure_targets(['http://example.test'], directory=tmp_path, timeout=.5):
        pass


from test_prerequisites import setup as prerequisite_setup


def test_measurements_keep_overlapping_service_leases_and_last_consumer_cleanup(helper, tmp_path, prerequisite_setup):
    from test_tool_fixture import server, get
    create, engine, _, _, _ = prerequisite_setup
    first, second = create(), create()
    first_lease = first.acquire()
    second_lease = second.acquire()
    assert second_lease.reused_ids == first_lease.owned_ids
    identifier, = first_lease.owned_ids
    measurements = []

    with server(tmp_path / 'backend') as endpoint:
        async def measurement(manager):
            async with helper.measure_targets([endpoint + '/graphql'], directory=tmp_path / 'measurements'):
                before = get(endpoint, '/control/value')
                # Let the peer contend while both already hold their ordinary lease.
                await asyncio.sleep(0)
                get(endpoint, '/hello')
                after = get(endpoint, '/control/value')
                assert after['backend_id'] == before['backend_id']
                assert after['reads'] - before['reads'] == 1
                measurements.append(after['reads'])
            # Cleanup starts only after measurement ownership is released.
            async with helper.measure_targets([endpoint], directory=tmp_path / 'measurements', timeout=.5):
                if manager is first:
                    assert manager.release() == ()
                    assert engine.records[identifier]['State']['Running']
                else:
                    assert manager.release() == first_lease.owned_ids
                    assert not engine.records[identifier]['State']['Running']

        async def run():
            await asyncio.gather(measurement(first), measurement(second))
        try:
            asyncio.run(run())
        finally:
            first.release()
            second.release()
    assert measurements == [1, 2]


@pytest.mark.parametrize('driver', ['test_tool_instances.py', 'test_tool_alternate.py'])
def test_driver_imports_sibling_helper_from_relocated_resources_without_checkout(driver, tmp_path):
    import shutil
    root = tmp_path / 'installed-resources'
    destination = root / 'tests/integration'
    fixtures = destination / 'fixtures/tool_instances'
    fixtures.mkdir(parents=True)
    shutil.copyfile(SOURCE, fixtures / 'measurement.py')
    shutil.copyfile(ROOT / 'tests/integration' / driver, destination / driver)
    script = '''
import inspect, runpy, sys
namespace = runpy.run_path(sys.argv[1])
assert inspect.unwrap(namespace['measure_targets']).__code__.co_filename == sys.argv[2]
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(destination / driver), str(fixtures / 'measurement.py')],
        cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_real_workflow_measurement_packet_keeps_complete_recorded_ancestry(helper, tmp_path):
    from api_agent.providers.progress import progress_sink
    from api_agent.testing.recorded import _WorkflowStepBridge
    from api_agent.workflows.definition import load_definition, load_native_catalog
    from api_agent.workflows.execution import NodeFunction, Workflow
    from test_workflow_definition import make_graph
    root = tmp_path / 'graph'; root.mkdir()
    definition = load_definition(make_graph(root), base_root=root, pack_root=root)
    source = definition.layout.steps[0].source
    native = load_native_catalog(definition, source)
    events, progress = [], []
    workflow = Workflow(definition, {'inspect': NodeFunction(native['inspect'], 'deterministic')},
                        services={'observe': events.append})
    with progress_sink(progress.append):
        async with helper.measure_targets(['http://example.test'], directory=tmp_path / 'locks'):
            result = await workflow.arun({})
            assert result['inspect'] == {'seen': True}
    bridge = _WorkflowStepBridge()
    bridge.add({'graph_id': definition.graph_id, 'events': events})
    root_event = next(item for item in events if item['event'] == 'scope_start')
    assert root_event['parent_id'] is None
    assert any(item.name == 'Tool fixture measurement lock wait' for item in progress)
    assert len(bridge.steps) == 1
    assert bridge.steps[0]['name'] == 'graph GRAPH'
    assert bridge.steps[0]['status'] == 'passed'
    assert bridge.steps[0]['steps'][0]['name'] == 'node inspect'


@pytest.mark.asyncio
async def test_nested_workflow_keeps_real_parent_across_measurement_wait(helper, tmp_path):
    from api_agent.testing.recorded import _WorkflowStepBridge
    from api_agent.workflows.definition import load_definition, load_native_catalog
    from api_agent.workflows.execution import NodeFunction, Workflow
    from test_workflow_definition import make_graph
    inner_root, outer_root = tmp_path / 'inner', tmp_path / 'outer'
    inner_root.mkdir(); outer_root.mkdir()
    inner_definition = load_definition(make_graph(inner_root), base_root=inner_root, pack_root=inner_root)
    make_graph(outer_root)
    (outer_root / 'native.py').write_text(
        'async def inspect(inputs, context):\n    return await context.services["nested"]()\n')
    outer_definition = load_definition(outer_root / 'flow.md.html', base_root=outer_root, pack_root=outer_root)
    events = []
    inner_native = load_native_catalog(inner_definition, inner_definition.layout.steps[0].source)
    inner = Workflow(inner_definition, {'inspect': NodeFunction(inner_native['inspect'], 'deterministic')},
                     services={'observe': events.append})

    async def nested():
        async with helper.measure_targets(['http://example.test'], directory=tmp_path / 'locks'):
            return (await inner.arun({}))['inspect']

    outer_native = load_native_catalog(outer_definition, outer_definition.layout.steps[0].source)
    outer = Workflow(outer_definition, {'inspect': NodeFunction(outer_native['inspect'], 'deterministic')},
                     services={'observe': events.append, 'nested': nested})
    assert (await outer.arun({}))['inspect'] == {'seen': True}
    starts = [event for event in events if event['event'] == 'scope_start']
    graphs = [event for event in starts if event['kind'] == 'graph']
    parent = next(event for event in starts if event['kind'] == 'node')
    assert len(graphs) == 2
    assert graphs[0]['parent_id'] is None
    assert parent['parent_id'] == graphs[0]['scope_id']
    assert graphs[1]['parent_id'] == parent['scope_id']
    bridge = _WorkflowStepBridge()
    bridge.add({'graph_id': outer_definition.graph_id, 'events': events})
    assert len(bridge.steps) == 1
    assert bridge.steps[0]['steps'][0]['steps'][0]['name'] == 'graph GRAPH'
