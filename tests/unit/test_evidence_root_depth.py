"""Logical evidence depth is independent of the transport projection wrapper."""
import json
from uuid import uuid4

import pytest

from api_agent.providers.setup_transport import SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.capture import project_values
from api_agent.workflows.inference_capture import _snapshot_raw


@pytest.mark.asyncio
async def test_nested_provider_response_keeps_final_answer_through_local_journal():
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.workflows.journal import validate_journal
    from test_workflow_observations import config, definition

    credential = 'synthetic-selected-credential'
    raw = {'output': {'message': {'content': [
        {'reasoningContent': {'reasoningText': {
            'text': 'private reasoning ' + credential,
            'signature': 'opaque-signature-sentinel'}}},
        {'text': '{"answer":"safe final ' + credential + '"}'}]}}}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(credential,))
    snapshot, _ = _snapshot_raw({'provider_response': raw}, 4 * 1024 * 1024)
    frames = await SetupServer._prepare_isolated(
        [{'phase': 'response', 'content': snapshot, 'reason': 'captured'}], boundary)
    frame = json.loads(frames[0][2])
    assert frame['reason'] == 'captured'
    assert json.loads(frame['content']['provider_response']['output']['message']['content'][-1]['text']) == {
        'answer': 'safe final [REDACTED]'}

    rows = []
    adapter = WorkflowObservationFactory(config(True), boundary=boundary,
                                         sink=rows.append).create(definition())
    graph, visit, cause = str(uuid4()), str(uuid4()), str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=graph, owner='GRAPH', kind='graph'))
    adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph,
                         owner='Make', kind='node'))
    adapter.capture_json(visit_id=visit, direction='output', cause_id=cause,
                         capture_source='provider', values={'llm_response': frame['content']},
                         schema={'type': 'object'})
    adapter.observe(dict(event='scope_end', scope_id=visit, owner='Make', kind='node', outcome='passed'))
    adapter.observe(dict(event='scope_end', scope_id=graph, owner='GRAPH', kind='graph', outcome='passed'))
    journal = adapter.finalize()
    validate_journal(journal)
    captured = [row for row in rows if row.get('event') == 'capture']
    assert len(captured) == 1
    output = captured[0]['values']['llm_response']['provider_response']['output']['message']['content']
    assert json.loads(output[-1]['text']) == {'answer': 'safe final [REDACTED]'}
    from api_agent.runtime.local_diagnostics import LocalDiagnosticProjection
    displayed = json.loads(LocalDiagnosticProjection(full_content=True).format(captured[0]))
    shown = displayed['values']['llm_response']['provider_response']['output']['message']['content']
    assert json.loads(shown[-1]['text']) == {'answer': 'safe final [REDACTED]'}
    assert displayed['source_reason'] == 'captured'
    assert all(marker not in json.dumps(journal) + repr(rows)
               for marker in (credential, 'private reasoning', 'opaque-signature-sentinel'))
    assert all(marker not in json.dumps(displayed)
               for marker in (credential, 'private reasoning', 'opaque-signature-sentinel'))


@pytest.mark.asyncio
async def test_nested_provider_answer_is_captured_even_when_original_schema_rejects():
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.rich_inference import async_validate_result
    from test_response_policy_contract import request, result

    final_text = '{"answer":7}'
    raw = {'output': {'message': {'content': [
        {'reasoningContent': {'reasoningText': {'text': 'synthetic reasoning',
                                                'signature': 'opaque-signature'}}},
        {'text': final_text}]}}}
    boundary = EmissionBoundary(ContentPolicy(True))
    report = set()
    captured = project_values({'llm_response': {'provider_response': raw}},
                              {'type': 'object'}, boundary, report=report,
                              _provider_content=True)
    assert captured['llm_response']['provider_response']['output']['message']['content'][-1]['text'] == final_text
    assert 'synthetic reasoning' not in json.dumps(captured)
    assert report == set()
    original = request()
    with pytest.raises(ProviderAccessError, match='original schema'):
        await async_validate_result(original, result(original, final_text))


@pytest.mark.parametrize('case', ['deep', 'bytes', 'items'])
def test_provider_original_admission_retains_existing_bounds(case):
    value = {'text': 'visible'}
    if case == 'deep':
        for _ in range(21):
            value = {'nested': value}
    elif case == 'bytes':
        value = {'text': 'x' * 65537}
    else:
        value = {'items': [0] * 2049}
    report = set()
    assert project_values({'provider_response': value}, {'type': 'object'},
                          EmissionBoundary(ContentPolicy(True)), report=report,
                          _provider_content=True) == {}
    assert report == {'truncated'}


@pytest.mark.asyncio
@pytest.mark.parametrize('depth,reason', [(7, 'captured'), (8, 'captured'), (9, 'truncated')])
async def test_isolated_projection_uses_logical_content_depth(depth, reason):
    secret = 'synthetic-projection-sensitive-value'
    value = 'visible ' + secret
    for _ in range(depth - 1):
        value = {'nested': value}
    content = {'provider_payload': value}
    snapshot, size = _snapshot_raw(content, 4 * 1024 * 1024)
    assert size > 0
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    frames = await SetupServer._prepare_isolated(
        [{'phase': 'request', 'content': snapshot, 'reason': 'captured'}], boundary)
    assert len(frames) == 1
    frame = json.loads(frames[0][2])
    assert frame['status'] == 'evidence' and frame['reason'] == reason
    assert secret not in json.dumps(frame)
    if reason == 'captured':
        selected = frame['content']['provider_payload']
        for _ in range(depth - 1):
            selected = selected['nested']
        assert selected.startswith('visible ') and selected != 'visible ' + secret
        from api_agent.testing.payloads import PayloadStore
        class Recorder:
            def __init__(self):
                self.files = {}
            def record_artifact(self, name, data):
                self.files[name] = data
                return name
        recorder = Recorder()
        store = PayloadStore(recorder, 'synthetic-test')
        reference = store.put_wire(str(uuid4()), 'request', frame['content'], capture_source='provider')
        assert json.loads(recorder.files[reference['source']]) == frame['content']
        assert secret.encode() not in recorder.files[reference['source']]
    else:
        assert 'visible' not in json.dumps(frame['content'])


def test_normal_projection_keeps_its_original_depth_limit():
    boundary = EmissionBoundary(ContentPolicy(True))
    for depth, expected in ((8, 'captured'), (9, 'truncated')):
        value = 'visible'
        for _ in range(depth):
            value = {'nested': value}
        report = set()
        result = project_values(value, {'type': 'object'}, boundary, report=report)
        assert ('truncated' in report) == (expected == 'truncated')
        assert bool(result) == (expected == 'captured')


@pytest.mark.parametrize('flag', [True, 1, 100000, 'yes', None])
def test_invalid_internal_wrapper_is_rejected(flag):
    with pytest.raises(ValueError, match='Invalid internal content wrapper'):
        project_values({'not_values': {}}, {'type': 'object'}, EmissionBoundary(ContentPolicy(True)),
                       _content_wrapper=flag)


@pytest.mark.parametrize('flag', [1, 100000, 'yes', None])
def test_valid_wrapper_rejects_non_boolean_flag(flag):
    with pytest.raises(ValueError, match='Invalid internal content wrapper'):
        project_values({'values': {}}, {'type': 'object'}, EmissionBoundary(ContentPolicy(True)),
                       _content_wrapper=flag)


@pytest.mark.parametrize('count,reason', [(2048, 'captured'), (2049, 'truncated')])
def test_wrapped_and_direct_projection_share_logical_item_limit(count, reason):
    boundary = EmissionBoundary(ContentPolicy(True))
    content = {'items': [0] * (count - 2)}  # Root, list and each list element.
    direct_report, wrapped_report = set(), set()
    direct = project_values(content, {'type': 'object'}, boundary, report=direct_report)
    wrapped = project_values({'values': content}, {'type': 'object'}, boundary,
                             report=wrapped_report, _content_wrapper=True).get('values', {})
    assert direct == wrapped
    assert ('truncated' in direct_report) == ('truncated' in wrapped_report) == (reason == 'truncated')
    assert bool(direct) == (reason == 'captured')


@pytest.mark.asyncio
@pytest.mark.parametrize('fields,reason', [(2046, 'captured'), (2047, 'captured'), (2048, 'truncated')])
async def test_isolated_projection_counts_fields_as_logical_items(fields, reason):
    boundary = EmissionBoundary(ContentPolicy(True))
    content = {f'key_{index}': index for index in range(fields)}
    frames = await SetupServer._prepare_isolated(
        [{'phase': 'request', 'content': content, 'reason': 'captured'}], boundary)
    frame = json.loads(frames[0][2])
    assert frame['reason'] == reason
    assert frame['content'] == (content if reason == 'captured' else {})


@pytest.mark.asyncio
@pytest.mark.parametrize('depth,item_count,reason', [
    (8, None, 'captured'), (9, None, 'truncated'),
    (None, 2048, 'captured'), (None, 2049, 'truncated'),
])
async def test_real_setup_channel_to_payload_recording_retains_depth_and_redaction(depth, item_count, reason, tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    from api_agent.testing.payloads import PayloadResolver, PayloadStore
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    from test_payloads import recorder as make_recorder, result as make_result

    secret = 'synthetic-deep-credential'
    value = 'visible ' + secret
    if item_count is not None:
        # Content root, provider_payload dict, list and each list element count.
        value = {'items': [0] * (item_count - 4) + [value]}
    else:
        for _ in range(depth - 1):
            value = {'nested': value}
    source = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', source)
    expected = LLMResult(Message('assistant', (TextContent('done'),)))

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'provider_payload': value}, request.request_id)
            return expected

    registry = SetupRegistry('op', 'selected', Owner)
    server = SetupServer(registry, 'synthetic-depth-capability-000000000', timeout=10,
                         authority=EmissionBoundary(ContentPolicy(True)))
    descriptor = await server.start()
    def call():
        client = SetupClient(descriptor, timeout=10)
        try:
            request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
            assert client.complete(request, authority_scope=scope) == expected
            return list(client.evidence)
        finally:
            client.close()
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(call), 15)
        row = next(row for row in rows if row['phase'] == 'request')
        assert row['reason'] == reason
        assert secret not in json.dumps(rows)
        if reason == 'captured':
            selected = row['content']['provider_payload']
            if item_count is not None:
                assert selected['items'] == [0] * (item_count - 4) + ['visible [REDACTED]']
            else:
                for _ in range(depth - 1):
                    selected = selected['nested']
                assert selected == 'visible [REDACTED]'
        else:
            assert 'visible' not in json.dumps(row['content'])
        recorder = make_recorder(tmp_path)
        store = PayloadStore(recorder, 'test')
        ref = store.put_wire(str(uuid4()), 'request', row['content'], reason=row['reason'],
                             transport_attempt_id=row.get('transport_attempt_id'),
                             capture_source=row.get('capture_source'))
        if reason == 'captured':
            assert ref is not None
            assert json.loads((tmp_path / ref['source']).read_bytes()) == row['content']
            assert secret.encode() not in (tmp_path / ref['source']).read_bytes()
        elif depth == 9:
            assert ref is not None
            assert json.loads((tmp_path / ref['source']).read_bytes()) == row['content']
            assert 'visible' not in (tmp_path / ref['source']).read_text()
        else:
            assert ref is None and store.index()['entries'][0]['reason'] == 'truncated'
        recorder.record_test(make_result())
        manifest = recorder.finalize()
        if ref is not None:
            resolver = PayloadResolver.local(tmp_path, manifest)
            try:
                assert resolver(ref) == row['content']
            finally:
                resolver.close()
        assert scope._leases == 0
    finally:
        await server.aclose()
        await registry.aclose()
        scope.close()
