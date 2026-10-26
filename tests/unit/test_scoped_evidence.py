"""Independent producer and consumer checks for scoped JSON evidence."""
import json
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.evidence import publish, validate_attachment
from api_agent.testing.inference_content import content_boundary


@pytest.fixture
def channel(monkeypatch, tmp_path):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'tests/test_example.py::test_example (call)')
    return path


def packet(path):
    envelope = json.loads(path.read_text())
    assert envelope['kind'] == 'workflow_evidence'
    assert envelope['node'].startswith('tests/unit/test_scoped_evidence.py::')
    assert not envelope['node'].endswith(' (call)')
    return envelope['data']


@pytest.mark.parametrize('kind,keys', [('test', ()), ('graph', ('invocation_id',)),
    ('scope', ('invocation_id', 'scope_id')), ('call', ('call_id',)), ('trial', ('trial_index',))])
def test_round_trip_owner_and_nested_values(channel, kind, keys):
    boundary = EmissionBoundary(ContentPolicy(True))
    ids = {key: 3 if key == 'trial_index' else str(uuid4()) for key in keys}
    value = {'visible': [None, True, 4, 2.5, {'text': 'example'}]}

    publish('Result', value, boundary=boundary, **ids)
    owner, attachment = validate_attachment(packet(channel), boundary, True)

    assert owner == {'kind': kind, **ids}
    assert attachment[0] == 'Result'
    assert json.loads(attachment[1]) == value
    assert attachment[2] == 'application/json'


def test_credentials_and_hidden_reasoning_are_scrubbed_before_ipc(channel):
    boundary = content_boundary({'LOG_CONTENT': True, 'API_KEY': 'configured-secret'})
    boundary.register_secret('dynamic-secret')

    publish('Result configured-secret dynamic-secret',
            {'visible': 'configured-secret dynamic-secret', 'password': 'never-record',
             'hidden_reasoning': 'private-thought', 'nested': {'text': 'Bearer bearer-secret'}},
            boundary=boundary)

    encoded = channel.read_text()
    for confidential in ('configured-secret', 'dynamic-secret', 'never-record', 'private-thought', 'bearer-secret'):
        assert confidential not in encoded
    owner, attachment = validate_attachment(packet(channel), boundary, True)
    assert owner == {'kind': 'test'}
    assert attachment[0].startswith('Result')
    assert 'visible' in json.loads(attachment[1])


@pytest.mark.parametrize('trace', [False, True])
def test_disabled_content_keeps_safe_title_and_withholding_marker(channel, trace):
    boundary = content_boundary({'LOG_CONTENT': False, 'TRACELOOP_TRACE_CONTENT': trace, 'API_KEY': 'label-secret'})

    publish('Result label-secret', {'text': 'confidential-value'}, boundary=boundary)
    data = packet(channel)
    _, attachment = validate_attachment(data, boundary, False)

    assert data['content'] == 'withheld'
    assert 'value' not in data
    assert 'confidential-value' not in channel.read_text()
    assert 'label-secret' not in attachment[0]
    assert attachment[0].startswith('Result')
    assert json.loads(attachment[1]) == {'content': 'withheld'}


@pytest.mark.parametrize('value', [float('nan'), float('inf'), object(), (1,), {1: 'x'},
    1 << 53, 'x' * 65_537, [0] * 2_049])
def test_invalid_json_is_rejected_before_writing(channel, value):
    with pytest.raises(ValueError):
        publish('Result', value, boundary=EmissionBoundary(ContentPolicy(True)))
    assert not channel.exists()


def test_cycle_and_depth_are_rejected(channel):
    cycle = []
    cycle.append(cycle)
    deep = None
    for _ in range(18):
        deep = [deep]
    for value in (cycle, deep):
        with pytest.raises(ValueError):
            publish('Result', value, boundary=EmissionBoundary(ContentPolicy(True)))
    assert not channel.exists()


@pytest.mark.parametrize('kwargs', [{'invocation_id': 'invalid'}, {'call_id': uuid4()},
    {'call_id': str(uuid4()).upper()}, {'scope_id': str(uuid4())}, {'trial_index': True},
    {'trial_index': 0}, {'trial_index': 61}, {'call_id': str(uuid4()), 'trial_index': 1},
    {'invocation_id': str(uuid4()), 'call_id': str(uuid4())}])
def test_invalid_owner_is_rejected_before_writing(channel, kwargs):
    with pytest.raises(ValueError):
        publish('Result', {}, boundary=EmissionBoundary(ContentPolicy(True)), **kwargs)
    assert not channel.exists()


@pytest.mark.parametrize('mutation', [
    lambda row: row.update(extra=True), lambda row: row.update(version=True),
    lambda row: row.update(content='unknown'), lambda row: row['owner'].update(extra=True),
    lambda row: row.update(owner={'kind': 'graph', 'invocation_id': 'invalid'}),
    lambda row: row.update(value={'hidden_reasoning': 'must-reject'}),
    lambda row: row.update(name='Result secret-value'),
    lambda row: row.update(value={'text': 'secret-value'}),
    lambda row: row.update(value=float('nan')),
])
def test_consumer_revalidates_untrusted_packet(channel, mutation):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('secret-value',))
    publish('Result', {'text': 'visible'}, boundary=boundary)
    row = packet(channel)
    mutation(row)
    with pytest.raises(ValueError):
        validate_attachment(row, boundary, True)


@pytest.mark.parametrize('boundary_enabled,allow_content', [(True, False), (False, True)])
def test_consumer_rejects_content_when_either_policy_disables_it(channel, boundary_enabled, allow_content):
    publish('Result', {'text': 'visible'}, boundary=EmissionBoundary(ContentPolicy(True)))
    with pytest.raises(ValueError):
        validate_attachment(packet(channel), EmissionBoundary(ContentPolicy(boundary_enabled)), allow_content)


def test_without_integration_environment_no_file_is_created(monkeypatch):
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    assert publish('Result', {}, boundary=EmissionBoundary()) is None


def test_boundary_is_required(channel):
    with pytest.raises(ValueError):
        publish('Result', {}, boundary=None)
    assert not channel.exists()


@pytest.mark.parametrize('enabled', [False, True])
def test_producer_rejects_owner_matching_registered_credential(channel, enabled):
    identifier = str(uuid4())
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=(identifier,))
    with pytest.raises(ValueError, match='owner violates credential boundary'):
        publish('Result', {}, boundary=boundary, invocation_id=identifier)
    assert not channel.exists()


def test_consumer_rejects_owner_matching_registered_credential(channel):
    identifier = str(uuid4())
    publish('Result', {}, boundary=EmissionBoundary(ContentPolicy(True)), call_id=identifier)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(identifier,))
    with pytest.raises(ValueError, match='owner violates credential boundary'):
        validate_attachment(packet(channel), boundary, True)


def test_label_fallback_is_scrubbed_before_ipc(channel):
    boundary = EmissionBoundary(ContentPolicy(False), secrets=('Attachment',))
    with pytest.raises(ValueError, match='fallback name violates credential boundary'):
        publish('NaN', {}, boundary=boundary)
    assert not channel.exists()


@pytest.mark.parametrize('name', ['', '   ', None, 'x' * 1023])
def test_invalid_label_is_rejected_before_ipc(channel, name):
    with pytest.raises(ValueError):
        publish(name, {}, boundary=EmissionBoundary())
    assert not channel.exists()


def test_redacted_label_must_fit_label_budget(channel):
    boundary = EmissionBoundary(ContentPolicy(False), secrets=('x',))
    with pytest.raises(ValueError, match='name exceeds byte budget'):
        publish('x ' * 100, {}, boundary=boundary)
    assert not channel.exists()


def test_safe_fallback_label_is_retained(channel):
    boundary = EmissionBoundary(ContentPolicy(False))
    publish('NaN', {}, boundary=boundary)
    _, attachment = validate_attachment(packet(channel), boundary, False)
    assert attachment[0] == 'Attachment'


def test_invalid_unicode_is_rejected_before_ipc(channel):
    with pytest.raises(ValueError, match='valid Unicode'):
        publish('Result', '\ud800', boundary=EmissionBoundary(ContentPolicy(True)))
    assert not channel.exists()


@pytest.mark.parametrize('owner', [[], {'kind': 1}, {'kind': 'trial', 'trial_index': None}])
def test_consumer_rejects_malformed_owner_shape_and_identity(channel, owner):
    boundary = EmissionBoundary(ContentPolicy(True))
    publish('Result', {}, boundary=boundary)
    row = packet(channel)
    row['owner'] = owner
    with pytest.raises(ValueError, match='Invalid attachment owner'):
        validate_attachment(row, boundary, True)
