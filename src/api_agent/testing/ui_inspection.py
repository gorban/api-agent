"""Preserve xdist's original inspection workers until explicit cleanup acknowledgement."""
import json
from importlib.metadata import version
import os
from pathlib import Path
import time

import pytest

_nodes = []
_teardown = None


def pytest_configure(config):
    if (os.environ.get('API_AGENT_INTEGRATION_EVENTS')
            and (hasattr(config, 'workerinput') or config.getoption('numprocesses', default=0))):
        if version('pytest-xdist').split('.')[0] != '3':
            raise pytest.UsageError('Parallel DEBUG inspection requires pytest-xdist 3')


def _paths(worker):
    root = Path(os.environ['API_AGENT_INTEGRATION_EVENTS']).parent
    return root / (worker + '-inspection.jsonl'), root / (worker + '-inspection-command.json')


def _send(path, **row):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row) + '\n')


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    manifest = node.workeroutput.get('api_agent_inspection', ())
    if manifest:
        if (not isinstance(manifest, list) or len(manifest) > 256
                or any(not isinstance(endpoint, str) or len(endpoint) > 4096 for endpoint in manifest)):
            raise RuntimeError('Unsupported parallel inspection manifest')
        if not any(existing is node for existing, _ in _nodes):
            _nodes.append((node, manifest))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    global _teardown
    config = session.config
    active = bool(os.environ.get('API_AGENT_INTEGRATION_EVENTS'))
    worker = getattr(config, 'workerinput', None)
    if active and worker is not None:
        from .provider_setup import retained_inspection_manifest
        manifest = list(retained_inspection_manifest())
        config.workeroutput['api_agent_inspection'] = manifest
    elif active and _nodes:
        dsession = config.pluginmanager.getplugin('dsession')
        manager = getattr(dsession, 'nodemanager', None)
        if manager is None or not callable(getattr(manager, 'teardown_nodes', None)):
            raise RuntimeError('Parallel DEBUG inspection requires the supported pytest-xdist 3 lifecycle')
        if any(not callable(getattr(getattr(getattr(node.gateway, '_io', None), 'popen', None), 'poll', None))
               for node, _ in _nodes):
            raise RuntimeError('Parallel DEBUG inspection requires local supervised xdist workers')
        original = manager.teardown_nodes
        def finish():
            manager.teardown_nodes = original
            original()
        _teardown = finish
        manager.teardown_nodes = lambda: None
    yield
    if active and worker is not None and manifest:
        from .provider_setup import serve_inspections
        events, command = _paths(worker['workerid'])
        serve_inspections(lambda **row: _send(events, **row), command_path=command)


def finish_parallel_inspections(send):
    """Finalize reports first, then supervise retained workers on their original loops."""
    global _teardown
    if not _nodes:
        return
    command = Path(os.environ['API_AGENT_INTEGRATION_EVENTS']).parent / 'inspection-command.json'
    remaining = list(_nodes)
    offsets = {}
    seen = 0
    for node, endpoints in remaining:
        for endpoint in endpoints:
            send(kind='inspection_open', endpoint=endpoint)
    teardown_pending = False
    while remaining or _teardown is not None:
        attempt = None
        try:
            row = json.loads(command.read_text())
            if (isinstance(row, dict) and type(row.get('version')) is int and row['version'] == 1
                    and row.get('command') == 'end_inspection' and type(row.get('attempt')) is int
                    and row['attempt'] > seen):
                attempt = seen = row['attempt']
        except (OSError, ValueError):
            pass
        for entry in tuple(remaining):
            node, _ = entry
            events, destination = _paths(node.gateway.id)
            if attempt is not None:
                destination.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': attempt}))
            try:
                content = events.read_text()
            except OSError:
                content = ''
            complete = content.rfind('\n') + 1
            acknowledged = False
            for line in content[offsets.get(node.gateway.id, 0):complete].splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('kind') == 'inspection_cleanup_pending':
                    send(kind='inspection_cleanup_pending', attempt=event.get('attempt'))
                if event.get('kind') == 'inspection_closed':
                    acknowledged = True
            offsets[node.gateway.id] = complete
            if acknowledged:
                remaining.remove(entry)
            elif (node.channel.isclosed()
                  or node.gateway._io.popen.poll() is not None):
                send(kind='inspection_lost')
                remaining.remove(entry)
        if not remaining and _teardown is not None and (not teardown_pending or attempt is not None):
            try:
                _teardown()
            except Exception:
                teardown_pending = True
                send(kind='inspection_cleanup_pending', attempt=seen)
            else:
                _teardown = None
        if remaining or _teardown is not None:
            time.sleep(.03)
    _nodes.clear()
    send(kind='inspection_closed', attempt=seen)
