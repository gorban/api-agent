"""Explicit UI turn identity survives immutable transport and final accounting."""
from copy import deepcopy
import json
from uuid import UUID

import pytest

from api_agent.testing import browser_attachments as transport
from api_agent.testing.browser_evidence import PNG_SIGNATURE
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.evidence_placement import place_evidence
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import run_recorded_integration
from test_recorded_integration import fixture, Storage


def uid(number):
    return str(UUID(int=number))


def owner(chat=1, turn=2):
    return dict(kind='turn', conversation_id=uid(chat), turn_id=uid(turn))


@pytest.mark.parametrize('change', [dict(kind='graph'), dict(turn_id='bad'),
    dict(conversation_id=uid(1).upper().replace('00000000', 'ABCDEF00', 1)),
    dict(turn_id=2), dict(extra='payload')])
def test_owner_schema_rejects_noncanonical_or_extra_fields(change):
    supplied = owner()
    supplied.update(change)
    with pytest.raises(ValueError):
        transport.validate_owner(supplied)


@pytest.mark.parametrize('matches', [0, 1, 2])
def test_turn_placement_requires_unique_exact_observed_wrapper(matches):
    owned = owner()
    wrapper = dict(name='User turn 1', parameters=[dict(name=key, value=value)
        for key, value in owned.items() if key != 'kind'], steps=[])
    steps = [deepcopy(wrapper) for _ in range(matches)]
    steps.append(dict(name='graph GRAPH', parameters=deepcopy(wrapper['parameters']), steps=[]))
    wrong_chat = deepcopy(wrapper)
    wrong_chat['parameters'][0]['value'] = uid(99)
    steps.append(wrong_chat)
    reference = dict(name='UI success context 1 page 1', source='one', type='image/png')
    queued = [(owned, reference)]
    first = place_evidence(steps, queued)
    assert place_evidence(steps, queued) == first
    if matches == 1:
        assert first == []
        assert steps[0]['attachments'] == [reference]
    else:
        assert [item['name'] for item in first] == ['Unattached ' + reference['name']]
        assert all(not step.get('attachments') for step in steps)


@pytest.mark.parametrize('status', ['passed', 'failed'])
def test_producer_transport_recording_relocates_turns_after_frozen_accounting(tmp_path, monkeypatch, status):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    channel = tmp_path / 'ui-events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(channel))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', selected.node_id + ' (call)')
    identities = [owner(1, 2), owner(1, 3), owner(4, 5)]
    for index, owned in enumerate(identities):
        transport.publish_attachment(('UI success context 1 page 1', PNG_SIGNATURE + bytes([index]), 'image/png'), owner=owned)
    packets = [json.loads(line)['data'] for line in channel.read_text().splitlines()]
    outcome = TestResult(selected, status, .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for descriptor in reversed(packets):
            attachment = transport.read_attachment(descriptor, tmp_path)
            hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
                text=json.dumps(descriptor), attachment=attachment))
            hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
                text=json.dumps(descriptor), attachment=attachment))
        for index, owned in enumerate(identities):
            base = dict(call_id=uid(10 + index), selection='synthetic-model',
                conversation_id=owned['conversation_id'], turn_id=owned['turn_id'],
                turn_kind='initial', attempt=1, started_at='2026-01-01T00:00:00+00:00')
            for usage in (None, dict(input_tokens=1, output_tokens=2, cached_input_tokens=0,
                                    cache_write_tokens=0, reasoning_tokens=0)):
                data = dict(base, usage=usage, finished_at='2026-01-01T00:00:01+00:00' if usage else None)
                if usage:
                    data.update(outcome='completed', quote={'list_cost_usd': .01, 'payment': None})
                hooks['on_event'](RunnerEvent('inference_usage', identity=selected, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(int(status == 'failed'), (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    found = {}
    for chat in row['steps']:
        for turn in chat['steps']:
            params = {item['name']: item['value'] for item in turn['parameters']}
            images = [item for item in turn.get('attachments', []) if item['type'] == 'image/png']
            assert len(images) == 1
            found[(params['conversation_id'], params['turn_id'])] = (result.directory / images[0]['source']).read_bytes()
    assert found == {(owned['conversation_id'], owned['turn_id']): PNG_SIGNATURE + bytes([index])
                     for index, owned in enumerate(identities)}
    assert not any(item['type'] == 'image/png' for item in row['attachments'])
    assert result.usage['test_costs'][selected.id]['list_cost_usd'] == pytest.approx(.03)


def test_owner_revalidation_consults_live_registered_credentials():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    boundary = EmissionBoundary(ContentPolicy(True))
    assert transport.validate_owner(owner(), boundary) == owner()
    boundary.register_secret(uid(2))
    with pytest.raises(ValueError, match='credential boundary'):
        transport.validate_owner(owner(), boundary)


@pytest.mark.parametrize('change', [dict(owner={'kind': 'turn', 'turn_id': uid(2)}),
    dict(payload='untrusted'), dict(name='../outside')])
def test_descriptor_revalidation_rejects_spoofed_metadata(tmp_path, monkeypatch, change):
    channel = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(channel))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'tests/test_ui.py::test_ui (call)')
    transport.publish_attachment(('UI success context 1 page 1', PNG_SIGNATURE, 'image/png'), owner=owner())
    descriptor = json.loads(channel.read_text())['data']
    descriptor.update(change)
    with pytest.raises(ValueError):
        transport.read_attachment(descriptor, tmp_path)


@pytest.mark.parametrize('budget', ['packets', 'bytes'])
def test_recording_bounds_aggregate_browser_receipts_with_one_budget_marker(tmp_path, budget):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    outcome = TestResult(selected, 'passed', .01)

    payloads = ([PNG_SIGNATURE] * 80 if budget == 'packets' else
                [PNG_SIGNATURE + b'x' * (4 * 1024 * 1024 - 9)] * 4 +
                [PNG_SIGNATURE + b'cross'] + [PNG_SIGNATURE] * 10)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for pixels in payloads:
            hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
                attachment=('UI success context 1 page 1', pixels, 'image/png')))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    images = [item for item in row['attachments'] if item['type'] == 'image/png']
    assert len(images) == (64 if budget == 'packets' else 4)
    markers = [item for item in row['attachments'] if item['name'] == 'UI failure context 0 page 0']
    assert len(markers) == 1
    assert json.loads((result.directory / markers[0]['source']).read_bytes())['reason'] == 'budget_exceeded'


def test_recording_retains_receipt_keys_only_for_admitted_packets(tmp_path):
    import hashlib
    import inspect
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    outcome = TestResult(selected, 'passed', .01)
    supplied = ('UI success context 1 page 1', PNG_SIGNATURE, 'image/png')

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for index in range(1000):
            descriptor = dict(name=f'{index:032x}.evidence', title=supplied[0], mime=supplied[2],
                sha256=hashlib.sha256(supplied[1]).hexdigest(), owner=owner())
            hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
                text=json.dumps(descriptor), attachment=supplied))
        retained = inspect.getclosurevars(hooks['on_event']).nonlocals['browser_receipts']
        assert len(retained) == 64
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    run_recorded_integration(context, [selected], config, accepted=True, runner=runner, storage=Storage())


def test_normal_subprocess_pipeline_preserves_turn_images_in_final_manifest(tmp_path):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_ui.py').write_text('''import json, os
from pathlib import Path
from uuid import UUID
from api_agent.testing.browser_attachments import publish_attachment
from api_agent.testing.browser_evidence import PNG_SIGNATURE

def test_ui():
    channel = Path(os.environ['API_AGENT_INTEGRATION_EVENTS'])
    node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
    for index, (chat, turn) in enumerate(((1, 2), (1, 3), (4, 5))):
        chat, turn = str(UUID(int=chat)), str(UUID(int=turn))
        publish_attachment(('UI success context 1 page 1', PNG_SIGNATURE + bytes([index]), 'image/png'),
            owner=dict(kind='turn', conversation_id=chat, turn_id=turn))
        base = dict(call_id=str(UUID(int=10 + index)), selection='synthetic-model',
            conversation_id=chat, turn_id=turn, turn_kind='initial', attempt=1,
            started_at='2026-01-01T00:00:00+00:00')
        for usage in (None, dict(input_tokens=1, output_tokens=2, cached_input_tokens=0,
                                cache_write_tokens=0, reasoning_tokens=0)):
            data = dict(base, usage=usage, finished_at='2026-01-01T00:00:01+00:00' if usage else None)
            if usage:
                data.update(outcome='completed', quote={'list_cost_usd': .01, 'payment': None})
            with channel.open('a') as stream:
                stream.write(json.dumps(dict(kind='inference_usage', node=node, data=data)) + '\\n')
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: [suite])
    config = dict(TUI_MODE='integration', LOG_CONTENT=True, ALLURE_S3_BUCKET='synthetic',
                  ALLURE_RESULTS_DIR=tmp_path / 'artifacts', TEST_PARALLEL_JOBS=0)
    selected = collect_tests(context, 'integration', config=config)
    result = run_recorded_integration(context, selected, config, accepted=True, storage=Storage())
    assert result.exit_code == 0
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    found = {}
    for chat in row['steps']:
        for turn in chat['steps']:
            params = {item['name']: item['value'] for item in turn['parameters']}
            image, = [item for item in turn.get('attachments', []) if item['type'] == 'image/png']
            assert image['source'] in result.manifest['files']
            found[(params['conversation_id'], params['turn_id'])] = (result.directory / image['source']).read_bytes()
    assert found == {(uid(chat), uid(turn)): PNG_SIGNATURE + bytes([index])
                     for index, (chat, turn) in enumerate(((1, 2), (1, 3), (4, 5)))}
    assert not any(item['type'] == 'image/png' for item in row['attachments'])


@pytest.mark.parametrize('budget', ['packets', 'bytes'])
def test_normal_worker_stops_file_reads_after_packet_or_byte_exhaustion(tmp_path, monkeypatch, budget):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    payloads = ("[('application/json', marker)] * 70" if budget == 'packets' else
                "[('image/png', PNG_SIGNATURE + b'x' * (4 * 1024 * 1024 - 9))] * 4 + "
                "[('image/png', PNG_SIGNATURE + b'cross')] + [('application/json', marker)] * 10")
    (suite / 'test_ui.py').write_text('''import json
from api_agent.testing.browser_attachments import publish_attachment
from api_agent.testing.browser_evidence import PNG_SIGNATURE

def test_ui():
    marker = json.dumps(dict(status='unavailable', phase='failure', context=1, page=1)).encode()
    for mime, content in ''' + payloads + ''':
        publish_attachment(('UI failure context 1 page 1', content, mime))
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: [suite])
    config = dict(LOG_CONTENT=True)
    selected = collect_tests(context, 'integration', config=config)
    read = transport.read_attachment
    reads = []
    limit = 64 if budget == 'packets' else 5

    def guarded_read(descriptor, directory):
        assert len(reads) < limit, 'Worker read a file after its screenshot budget was exhausted'
        reads.append(descriptor['name'])
        return read(descriptor, directory)

    monkeypatch.setattr(transport, 'read_attachment', guarded_read)
    events = []
    result = run_integration_tests(context, selected, accepted=True, config=config, on_event=events.append)
    assert result.exit_code == 0
    assert len(reads) == limit
    attachments = [event.attachment for event in events if event.kind == 'browser_attachment']
    markers = [item for item in attachments if item[0] == 'UI failure context 0 page 0']
    assert len(markers) == 1
    assert json.loads(markers[0][1])['reason'] == 'budget_exceeded'
    assert len(attachments) == (65 if budget == 'packets' else 5)


def test_matching_graph_without_observed_conversation_keeps_turn_image_unattached(tmp_path):
    import hashlib
    from test_recorded_payload_projection import packet
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    journal, request, response = packet(uid(10), uid(2), selected.node_id)
    image = ('UI success context 1 page 1', PNG_SIGNATURE, 'image/png')
    descriptor = dict(name='0' * 32 + '.evidence', title=image[0], mime=image[2],
        sha256=hashlib.sha256(image[1]).hexdigest(), owner=owner())
    outcome = TestResult(selected, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for phase, value in [('request', request), ('response', response)]:
            hooks['on_event'](RunnerEvent('inference_content', identity=selected, text=json.dumps(
                dict(call_id=uid(10), phase=phase, content=value, reason='captured'))))
        hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(journal)))
        hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
            text=json.dumps(descriptor), attachment=image))
        base = dict(call_id=uid(10), selection='synthetic-model', conversation_id=uid(1), turn_id=uid(2),
            turn_kind='initial', attempt=1, started_at='2026-01-01T00:00:00+00:00')
        for usage in (None, dict(input_tokens=1, output_tokens=2, cached_input_tokens=0,
                                cache_write_tokens=0, reasoning_tokens=0)):
            data = dict(base, usage=usage, finished_at='2026-01-01T00:00:01+00:00' if usage else None)
            if usage:
                data.update(outcome='completed', quote={'list_cost_usd': .01, 'payment': None})
            hooks['on_event'](RunnerEvent('inference_usage', identity=selected, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    projection = journal['projection']
    result = run_recorded_integration(context, [selected], config, accepted=True, runner=runner,
        storage=Storage(), workflow_admissions={projection['definition_id']: projection})
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    images = [item for item in row['attachments'] if item['type'] == 'image/png']
    assert len(images) == 1 and images[0]['name'] == 'Unattached ' + image[0]
    assert (result.directory / images[0]['source']).read_bytes() == image[1]
    assert all(not step['name'].startswith(('User turn ', 'Conversation ')) for step in row['steps'])
    assert result.usage['test_costs'][selected.id]['list_cost_usd'] == pytest.approx(.01)


@pytest.mark.parametrize('damage', ['pixels', 'title', 'mime', 'hash', 'credential', None])
def test_recorder_revalidates_attachment_and_legacy_descriptor(tmp_path, damage):
    import hashlib
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    supplied = ('UI success context 1 page 1', PNG_SIGNATURE, 'image/png')
    descriptor = dict(name='0' * 32 + '.evidence', title=supplied[0], mime=supplied[2],
                      sha256=hashlib.sha256(supplied[1]).hexdigest())
    if damage == 'pixels':
        supplied = (supplied[0], b'invalid', supplied[2])
    elif damage == 'title':
        descriptor['title'] = 'UI success context 1 page 2'
    elif damage == 'mime':
        descriptor['mime'] = 'application/json'
    elif damage == 'hash':
        descriptor['sha256'] = '0' * 64
    elif damage == 'credential':
        descriptor['owner'] = owner()
    outcome = TestResult(selected, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('browser_attachment', identity=selected,
            text=json.dumps(descriptor), attachment=supplied))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    options = dict(accepted=True, runner=runner, storage=Storage(),
                   secret_values=(uid(2),) if damage == 'credential' else ())
    if damage is not None:
        with pytest.raises(ValueError):
            run_recorded_integration(context, [selected], config, **options)
    else:
        result = run_recorded_integration(context, [selected], config, **options)
        row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
        image, = [item for item in row['attachments'] if item['type'] == 'image/png']
        assert (result.directory / image['source']).read_bytes() == PNG_SIGNATURE
