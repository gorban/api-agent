"""Synthetic large inference through the actual integration worker and recorder."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.recorded import run_recorded_integration


ROOT = Path(__file__).resolve().parents[2]
SECRET = 'synthetic-provider-credential-39'
REQUEST_PART = 'request-part-' + 'q' * (30 * 1024) + ' [REDACTED]'
RESPONSE = 'response-start-' + 'r' * (205 * 1024) + '-response-end [REDACTED]'

PROVIDER = '''
import os
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
class Auth:
    async def __call__(self): return 'synthetic-provider-credential-39'
    async def aclose(self): pass
class Provider:
    version = 1
    credential_protection_version = 1
    def __init__(self, config): self.config = config
    capabilities = ProviderCapabilities(kind='llm')
    def bind_credential_authority(self, authority): self.authority = authority
    async def asetup(self, **kwargs):
        await self.authority.aregister_secret('synthetic-provider-credential-39')
        return Auth()
    async def acomplete(self, request, *, auth):
        with open(self.config['SYNTHETIC_PIDS'], 'a') as stream: stream.write(str(os.getpid()) + '\\n')
        assert auth == 'synthetic-provider-credential-39'
        assert len(request.payload.messages) == 8
        assert all(item.content[0].text.startswith('request-part-') for item in request.payload.messages)
        return LLMResult(Message('assistant', (TextContent('response-start-' + 'r' * (205 * 1024) + '-response-end synthetic-provider-credential-39'),)), Usage(2, 3))
    async def aclose(self): pass
def factory(config, services): return Provider(config)
'''

SUITE = '''
import asyncio
import pytest
from api_agent.runtime.tasks import offload
from api_agent.workflows.application import build

@pytest.mark.credential_authorities('workflow')
def test_graph(llm_provider, integration_config):
    async def complete(request):
        return await offload(llm_provider.complete, request)
    graph = build(ROOT, llm=complete, selection='synthetic-model', config=integration_config.values)
    with open(integration_config.values['SYNTHETIC_WORKER_PID'], 'w') as stream: stream.write(str(__import__('os').getpid()))
    for _ in range(2):
        result = asyncio.run(graph.arun({'messages': [{'role': 'user', 'content': 'request-part-' + 'q' * (30 * 1024) + ' synthetic-provider-credential-39'} for _ in range(8)]}))
        assert result['llm_call']['response_text'].endswith('-response-end synthetic-provider-credential-39')
'''

class Storage:
    def persist(self, directory, manifest):
        self.manifest = manifest


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_large_inference_reaches_finalized_graph_evidence(tmp_path, monkeypatch, log, trace):
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, (str(ROOT / 'src'), os.environ.get('PYTHONPATH')))))
    pack = tmp_path / 'pack'
    suite = pack / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_graph.py').write_text('ROOT = ' + repr(str(ROOT)) + '\n' + SUITE)
    provider = tmp_path / 'provider.py'
    provider.write_text(PROVIDER)
    context = SimpleNamespace(base_root=ROOT, owner_root=pack, workflow_entry=None, workflow_replacements=(), workflow_root="base", pack_root=pack, suite_paths=lambda mode: (suite,))
    identity = TestIdentity('pack::tests/integration/test_graph.py::test_graph', 'pack',
        'tests/integration/test_graph.py', 'tests/integration/test_graph.py::test_graph',
        tags=frozenset({'credential_authorities'}))
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'results', 'TEST_PARALLEL_JOBS': 0,
        'LLM_PROVIDER': str(provider) + ':factory',
        'LOG_CONTENT': log, 'TRACELOOP_TRACE_CONTENT': trace,
        'SYNTHETIC_PIDS': str(tmp_path / 'provider-pids'), 'SYNTHETIC_WORKER_PID': str(tmp_path / 'worker-pid')}
    result = run_recorded_integration(context, [identity], config, accepted=True, storage=Storage())
    assert result.execution.exit_code == 0, result.execution.output
    assert result.persisted
    assert (tmp_path / 'provider-pids').read_text().splitlines() == [str(os.getpid())] * 2
    assert int((tmp_path / 'worker-pid').read_text()) != os.getpid()
    artifacts = list(result.directory.glob('workflow-0-*.json'))
    assert len(artifacts) == 2
    journals = [json.loads(path.read_text()) for path in artifacts]
    assert all(journal['projection']['graph_id'] == 'simple_chat' for journal in journals)
    assert len({journal['invocation_id'] for journal in journals}) == 2
    report = json.loads(next(result.directory.glob('*-result.json')).read_text())
    graphs = report['steps']
    assert len(graphs) == 2
    index = json.loads((result.directory / 'payload-index-0.json').read_text())
    entries = index['entries']
    assert len(entries) == (4 if log else 0)
    assert SECRET not in ''.join(path.read_text(errors='replace') for path in result.directory.iterdir() if path.is_file()), 'credential escaped provider boundary'
    part_sources, descriptor_sources = [], []
    for graph in graphs:
        source, = [item['source'] for item in graph['attachments'] if item['name'] == 'Execution evidence']
        journal = json.loads((result.directory / source).read_text())
        assert graph['name'] == 'graph GRAPH'
        component, = graph['steps']
        node, = component['steps']
        assert node['name'] == 'node llm_call'
        call, = [step for step in node['steps'] if step['name'].startswith('call ')]
        call_id = call['name'].removeprefix('call ')
        call_event, = [event for event in journal['events'] if event['event'] == 'call']
        assert call_event['call_id'] == call_id
        owned = [entry for entry in entries if entry['occurrence']['call_id'] == call_id]
        assert {entry['occurrence']['phase'] for entry in owned} == ({'request', 'response'} if log else set())
        assert all(entry['owner'] == {'invocation_id': journal['invocation_id'],
                                      'visit_id': call_event['visit_id']} for entry in owned)
        attempts = {next(item['value'] for item in step['parameters'] if item['name'] == 'transport_id'): step
                    for step in call['steps']}
        expected_attached = {attempt: [] for attempt in attempts}
        for entry in owned:
            descriptor_sources.append(entry['source'])
            manifest_bytes = (result.directory / entry['source']).read_bytes()
            assert hashlib.sha256(manifest_bytes).hexdigest() == entry['sha256']
            manifest = json.loads(manifest_bytes)
            chunks = manifest['chunks']
            assert len(chunks) >= 2
            expected_attached[entry['occurrence']['transport_attempt_id']].extend(chunk['source'] for chunk in chunks)
            part_sources.extend(chunk['source'] for chunk in chunks)
            pieces = [(result.directory / chunk['source']).read_bytes() for chunk in chunks]
            assert all(len(piece) == chunk['length'] and hashlib.sha256(piece).hexdigest() == chunk['sha256']
                       for piece, chunk in zip(pieces, chunks, strict=True))
            payload = b''.join(pieces)
            assert len(payload) == manifest['length'] == entry['length'] >= 200 * 1024
            assert hashlib.sha256(payload).hexdigest() == manifest['sha256']
            content = json.loads(payload)
            assert payload == json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
            if entry['occurrence']['phase'] == 'request':
                assert content['messages'] == [{'role': 'user', 'text': REQUEST_PART}] * 8
            else:
                assert content['text'] == RESPONSE
        for attempt, step in attempts.items():
            attached = [attachment['source'] for attachment in step.get('attachments', [])]
            assert len(attached) == len(set(attached))
            assert sorted(attached) == sorted(expected_attached[attempt])
    assert len(part_sources) == len(set(part_sources))
    assert len(descriptor_sources) == len(set(descriptor_sources))
    assert {path.name for path in result.directory.glob('payload-*.part')} == set(part_sources)
    assert {path.name for path in result.directory.glob('payload-*.json')} == set(descriptor_sources) | {'payload-index-0.json'}
    files = [path.read_bytes() for path in result.directory.iterdir() if path.is_file()]
    assert sum(content.count(b'request-part-') for content in files) == (16 if log else 0)
    assert sum(content.count(b'response-start-') for content in files) == (2 if log else 0)


def test_credential_redaction_depends_on_provider_registration(tmp_path, monkeypatch):
    from api_agent.telemetry.privacy import EmissionBoundary

    register = EmissionBoundary.register_secret
    def skip_synthetic_credential(boundary, value):
        if value != SECRET:
            return register(boundary, value)

    monkeypatch.setattr(EmissionBoundary, 'register_secret', skip_synthetic_credential)
    with pytest.raises(AssertionError, match='credential escaped provider boundary'):
        test_large_inference_reaches_finalized_graph_evidence(tmp_path, monkeypatch, True, False)


def test_real_worker_reports_bounded_evidence_overflow_without_leaking_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, (str(ROOT / 'src'), os.environ.get('PYTHONPATH')))))
    pack = tmp_path / 'pack'
    suite = pack / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_graph.py').write_text('ROOT = ' + repr(str(ROOT)) + '\n' + SUITE.replace('range(2)', 'range(10)'))
    provider = tmp_path / 'provider.py'
    provider.write_text(PROVIDER)
    context = SimpleNamespace(base_root=ROOT, owner_root=pack, workflow_entry=None,
        workflow_replacements=(), workflow_root='base', pack_root=pack, suite_paths=lambda mode: (suite,))
    identity = TestIdentity('pack::tests/integration/test_graph.py::test_graph', 'pack',
        'tests/integration/test_graph.py', 'tests/integration/test_graph.py::test_graph',
        tags=frozenset({'credential_authorities'}))
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'results', 'TEST_PARALLEL_JOBS': 0,
        'LLM_PROVIDER': str(provider) + ':factory', 'LOG_CONTENT': True,
        'SYNTHETIC_PIDS': str(tmp_path / 'provider-pids'), 'SYNTHETIC_WORKER_PID': str(tmp_path / 'worker-pid')}
    from api_agent.testing import inference_content
    original_read = inference_content.read_evidence
    original_initialize = inference_content.initialize_evidence
    observed, initialized, snapshots = [], [], []
    def inspect_initialize(path, nodes):
        initialized.append((path, tuple(nodes)))
        return original_initialize(path, nodes)
    def inspect_committed(path, after):
        rows = original_read(path, after)
        if rows:
            directory = Path(path).parent
            files = []
            for item in directory.glob('inference.sqlite*'):
                try:
                    files.append(item.read_bytes())
                except FileNotFoundError:
                    pass  # A completed SQLite transaction removed its journal.
            snapshots.append((rows[0][2], files, (directory / 'events.jsonl').read_bytes()))
        if rows and rows[0][2].get('reason') == 'truncated':
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                budgets = db.execute('SELECT node, bytes, rows, stopped FROM budget').fetchall()
                parts = db.execute('SELECT evidence_id, number, data FROM part ORDER BY evidence_id, number').fetchall()
            directory = Path(path).parent
            sidecars = [item.read_bytes() for item in directory.glob('inference.sqlite*') if item.is_file()]
            events = (directory / 'events.jsonl').read_bytes()
            observed.append((rows[0], budgets, parts, sidecars, events))
        return rows
    monkeypatch.setattr(inference_content, 'initialize_evidence', inspect_initialize)
    monkeypatch.setattr(inference_content, 'read_evidence', inspect_committed)
    result = run_recorded_integration(context, [identity], config, accepted=True, storage=Storage())
    assert result.execution.exit_code == 0, result.execution.output
    assert (tmp_path / 'provider-pids').read_text().splitlines() == [str(os.getpid())] * 10
    assert len(initialized) == 1 and initialized[0][1] == (identity.node_id,)
    assert len(snapshots) > 1
    for row, files, event_bytes in snapshots:
        assert SECRET not in json.dumps(row)
        assert all(SECRET.encode() not in blob for blob in files + [event_bytes])
        assert b'request-part-' not in event_bytes and b'response-start-' not in event_bytes
    marker, budgets, parts, sidecars, events = observed[0]
    assert len(observed) == 1 and marker[1] == identity.node_id
    assert marker[2]['reason'] == 'truncated' and marker[2]['content'] == {}
    assert len(budgets) == 1 and budgets[0][0] == identity.node_id
    assert 0 < budgets[0][1] <= 4 * 1024 * 1024 and budgets[0][3] == 1
    grouped = {}
    for evidence_id, _, data in parts:
        grouped.setdefault(evidence_id, []).append(data)
    rows = [json.loads(b''.join(chunks)) for chunks in grouped.values()]
    assert sum(len(json.dumps(row['content'], ensure_ascii=False, separators=(',', ':'),
                              allow_nan=False).encode('utf-8'))
               for row in rows if row.get('reason') != 'truncated') == budgets[0][1]
    assert all(0 < len(data) <= 200 * 1024 for _, _, data in parts)
    assert all(SECRET.encode() not in blob for blob in sidecars + [events])
    assert b'request-part-' not in events and b'response-start-' not in events
    assert all(row.get('kind') != 'inference_content' for row in map(json.loads, events.splitlines()))
    notice, = result.directory.glob('inference-evidence-incomplete-*.txt')
    assert 'truncated' in notice.read_text().lower()
    assert SECRET not in ''.join(path.read_text(errors='replace') for path in result.directory.iterdir() if path.is_file())
    index = json.loads((result.directory / 'payload-index-0.json').read_text())
    assert sum(entry.get('length', 0) for entry in index['entries']) <= 4 * 1024 * 1024
    assert len(index['entries']) < 20
