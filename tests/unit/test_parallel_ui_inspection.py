"""Per-worker lifecycle commands preserve aggregate inspection ownership."""
import json
from types import SimpleNamespace

import pytest

from api_agent.testing import ui_inspection as plugin


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(plugin, '_nodes', [])
    monkeypatch.setattr(plugin, '_teardown', None)
    return tmp_path


def node(worker, endpoints, *, closed=False):
    return SimpleNamespace(workeroutput={'api_agent_inspection': endpoints},
        gateway=SimpleNamespace(id=worker, _io=SimpleNamespace(popen=SimpleNamespace(poll=lambda: None))), channel=SimpleNamespace(isclosed=lambda: closed))


def test_aggregate_waits_for_both_worker_acknowledgements(channel, monkeypatch):
    first, second = node('gw0', ['http://localhost:1']), node('gw1', [''])
    plugin.pytest_testnodedown(first, None)
    plugin.pytest_testnodedown(second, None)
    (channel / 'inspection-command.json').write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
    (channel / 'gw0-inspection.jsonl').write_text('{"kind":"inspection_closed"}\n')
    slept = []
    def tick(delay):
        slept.append(delay)
        assert not any(row['kind'] == 'inspection_closed' for row in events)
        assert json.loads((channel / 'gw1-inspection-command.json').read_text())['attempt'] == 2
        (channel / 'gw1-inspection.jsonl').write_text('{"kind":"inspection_closed"}\n')
    monkeypatch.setattr(plugin.time, 'sleep', tick)
    events = []
    teardown = []
    monkeypatch.setattr(plugin, '_teardown', lambda: teardown.append(True))
    plugin.finish_parallel_inspections(lambda **row: events.append(row))
    assert slept and teardown == [True]
    assert [row['kind'] for row in events] == ['inspection_open', 'inspection_open', 'inspection_closed']
    assert plugin._nodes == []


def test_lost_worker_is_explicit_before_aggregate_closure(channel):
    plugin.pytest_testnodedown(node('gw0', [''], closed=True), None)
    events = []
    plugin.finish_parallel_inspections(lambda **row: events.append(row))
    assert [row['kind'] for row in events] == ['inspection_open', 'inspection_lost', 'inspection_closed']


@pytest.mark.parametrize('manifest', [[1], ['x' * 4097], [''] * 257])
def test_manifest_rejects_unbounded_or_non_endpoint_values(channel, manifest):
    with pytest.raises(RuntimeError, match='manifest'):
        plugin.pytest_testnodedown(node('gw0', manifest), None)


def test_cleanup_pending_attempt_is_forwarded_until_next_ack(channel, monkeypatch):
    plugin.pytest_testnodedown(node('gw0', ['']), None)
    (channel / 'inspection-command.json').write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 1}))
    (channel / 'gw0-inspection.jsonl').write_text('{"kind":"inspection_cleanup_pending","attempt":1}\n')
    events = []
    def tick(delay):
        assert events[-1] == {'kind': 'inspection_cleanup_pending', 'attempt': 1}
        assert plugin._nodes
        (channel / 'inspection-command.json').write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
        with (channel / 'gw0-inspection.jsonl').open('a') as stream:
            stream.write('{"kind":"inspection_closed","attempt":2}\n')
    monkeypatch.setattr(plugin.time, 'sleep', tick)
    plugin.finish_parallel_inspections(lambda **row: events.append(row))
    assert json.loads((channel / 'gw0-inspection-command.json').read_text())['attempt'] == 2
    assert events[-1] == {'kind': 'inspection_closed', 'attempt': 2}


def test_teardown_failure_keeps_ownership_for_retry(channel, monkeypatch):
    plugin.pytest_testnodedown(node('gw0', ['']), None)
    (channel / 'gw0-inspection.jsonl').write_text('{"kind":"inspection_closed"}\n')
    calls = []
    def teardown():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('controlled teardown failure')
    monkeypatch.setattr(plugin, '_teardown', teardown)
    def tick(delay):
        assert plugin._nodes
        assert events[-1]['kind'] == 'inspection_cleanup_pending'
        (channel / 'inspection-command.json').write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
    monkeypatch.setattr(plugin.time, 'sleep', tick)
    events = []
    plugin.finish_parallel_inspections(lambda **row: events.append(row))
    assert calls == [True, True]
    assert plugin._nodes == []
    assert events[-1] == {'kind': 'inspection_closed', 'attempt': 2}


def test_controller_defers_only_its_manager_until_explicit_finalization(channel):
    calls = []
    manager = SimpleNamespace(teardown_nodes=lambda: calls.append('teardown'))
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: SimpleNamespace(nodemanager=manager)))
    plugin.pytest_testnodedown(node('gw0', ['']), None)
    hook = plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
    next(hook)
    manager.teardown_nodes()
    assert calls == []
    with pytest.raises(StopIteration):
        next(hook)
    (channel / 'gw0-inspection.jsonl').write_text('{"kind":"inspection_closed"}\n')
    plugin.finish_parallel_inspections(lambda **row: None)
    assert calls == ['teardown']
    manager.teardown_nodes()
    assert calls == ['teardown', 'teardown']


def test_worker_publishes_manifest_before_xdist_finishes_then_serves_same_thread(channel, monkeypatch):
    import threading
    from api_agent.testing import provider_setup
    monkeypatch.setattr(provider_setup, 'retained_inspection_manifest', lambda: ('http://localhost:1',), raising=False)
    calls = []
    monkeypatch.setattr(provider_setup, 'serve_inspections', lambda send, **kw: calls.append((threading.get_ident(), kw)))
    config = SimpleNamespace(workerinput={'workerid': 'gw0'}, workeroutput={})
    hook = plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
    next(hook)
    assert config.workeroutput['api_agent_inspection'] == ['http://localhost:1']
    assert calls == []
    with pytest.raises(StopIteration):
        next(hook)
    assert calls == [(threading.get_ident(), {'command_path': channel / 'gw0-inspection-command.json'})]


def test_unsupported_controller_seam_fails_with_retained_manifest(channel):
    plugin.pytest_testnodedown(node('gw0', ['']), None)
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: None))
    with pytest.raises(RuntimeError, match='supported pytest-xdist'):
        next(plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0))
    assert plugin._nodes


@pytest.mark.parametrize('method', ['_watch', 'end_inspection'])
def test_aggregate_closed_cannot_hide_lost_original_worker(channel, monkeypatch, method):
    from unittest.mock import Mock
    from api_agent.testing import inspection
    monkeypatch.setattr(inspection.threading.Thread, 'start', lambda self: None)
    monkeypatch.setattr(inspection, 'disposal_complete', lambda process: True)
    events = channel / 'events.jsonl'
    events.write_text('{"kind":"inspection_lost"}\n{"kind":"inspection_closed"}\n')
    process = SimpleNamespace(poll=lambda: 0, stdout=SimpleNamespace(close=Mock()))
    directory = inspection.InspectionDirectory(SimpleNamespace(name=str(channel), cleanup=Mock()))
    reader = SimpleNamespace(join=Mock(), is_alive=lambda: False)
    handle = inspection.IntegrationInspection(process, directory, events, reader)
    getattr(handle, method)()
    assert handle.closed and handle.lost


def test_dead_worker_detected_even_when_descendants_keep_channel_open(channel):
    worker = node('gw0', [''])
    worker.gateway._io.popen.poll = lambda: -9
    plugin.pytest_testnodedown(worker, None)
    events = []
    plugin.finish_parallel_inspections(lambda **row: events.append(row))
    assert [row['kind'] for row in events] == ['inspection_open', 'inspection_lost', 'inspection_closed']


def test_remote_gateway_without_process_supervision_rejected(channel):
    worker = node('gw0', [''])
    del worker.gateway._io
    plugin.pytest_testnodedown(worker, None)
    manager = SimpleNamespace(teardown_nodes=lambda: None)
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: SimpleNamespace(nodemanager=manager)))
    with pytest.raises(RuntimeError, match='local supervised'):
        next(plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0))


def test_repeated_worker_down_notification_has_one_owner(channel):
    worker=node('gw0',[''])
    plugin.pytest_testnodedown(worker,None)
    plugin.pytest_testnodedown(worker,KeyboardInterrupt())
    assert len(plugin._nodes)==1 and plugin._nodes[0][0] is worker


def test_scalar_neighbors_and_partial_line_preserve_real_ack(channel,monkeypatch):
    plugin.pytest_testnodedown(node('gw0',['']),None)
    event=channel/'gw0-inspection.jsonl'
    event.write_text('bad\nnull\n[]\n3\n{"kind":"inspection_clo')
    events=[]
    def tick(delay):
        assert not any(x['kind']=='inspection_closed' for x in events)
        with event.open('a') as stream:stream.write('sed"}\n')
    monkeypatch.setattr(plugin.time,'sleep',tick)
    plugin.finish_parallel_inspections(lambda **row:events.append(row))
    assert events[-1]=={'kind':'inspection_closed','attempt':0}


@pytest.mark.parametrize('active,worker,jobs,major,reject',[(False,False,2,'3',False),(True,False,0,'4',False),
    (True,True,0,'3',False),(True,False,2,'4',True)])
def test_plugin_activation_requires_supported_parallel_lifecycle(channel,monkeypatch,active,worker,jobs,major,reject):
    if not active:monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS')
    config=SimpleNamespace(getoption=lambda *a,**kw:jobs)
    if worker:config.workerinput={}
    monkeypatch.setattr(plugin,'version',lambda name:major+'.0.0')
    if reject:
        with pytest.raises(pytest.UsageError):plugin.pytest_configure(config)
    else:plugin.pytest_configure(config)


def test_empty_manifest_and_no_nodes_do_not_defer_teardown(channel):
    from unittest.mock import Mock
    plugin.pytest_testnodedown(node('gw0',[]),None)
    send=Mock()
    plugin.finish_parallel_inspections(send)
    send.assert_not_called()
    plugin._send(channel/'rows',kind='inspection_closed')
    assert json.loads((channel/'rows').read_text())=={'kind':'inspection_closed'}


def test_inactive_session_hook_is_noop(channel,monkeypatch):
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS')
    hook=plugin.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()),0)
    next(hook)
    with pytest.raises(StopIteration):next(hook)
