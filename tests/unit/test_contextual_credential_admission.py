"""Evidence admission follows the enclosing export traversal's bounds."""
import json

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.recording import RunRecorder
from test_recording import identity, result


PROTECTED = {'password': 'owned-context-primary', 'opaque': 'owned-context-fragment'}
WRAPPERS = {'dictionary': lambda value: {'child': value},
            'list': lambda value: [value], 'tuple': lambda value: (value,)}


def nested(value, kind, count):
    for _ in range(count):
        value = WRAPPERS[kind](value)
    return value


def leaf(value, kind, count):
    for _ in range(count):
        value = value['child'] if kind == 'dictionary' else value[0]
    return value


@pytest.fixture(params=['root', *WRAPPERS])
def ancestor_cycle_payload(request):
    node = dict(PROTECTED)
    payload = {'duration_ms': 5, 'note': node}
    ancestor = payload
    if request.param != 'root':
        ancestor = WRAPPERS[request.param](node)
        payload['note'] = ancestor
    node['ignored'] = ancestor
    return payload


def test_ancestor_omission_precedes_registered_container_comparison(ancestor_cycle_payload):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(PROTECTED),))
    actual = boundary.export(ancestor_cycle_payload)
    assert actual['duration_ms'] == 5
    assert 'owned-context-fragment' not in json.dumps(actual)


@pytest.mark.parametrize('kind', WRAPPERS)
def test_native_depth_omission_precedes_registered_container_comparison(kind):
    node = dict(PROTECTED, ignored={'ordinary': 42})
    payload = {'note': nested(node, kind, 19), 'duration_ms': 5}
    plain = EmissionBoundary(ContentPolicy(True)).export(payload)
    assert leaf(plain['note'], kind, 19) == {'opaque': PROTECTED['opaque'], 'ignored': {}}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(dict(PROTECTED, ignored={})),))
    actual = boundary.export(payload)
    assert actual['duration_ms'] == 5
    assert 'owned-context-fragment' not in json.dumps(actual)


@pytest.mark.parametrize('kind,empty', [('dictionary', {}), ('list', []), ('tuple', ())])
def test_root_comparison_preserves_fields_at_actual_export_depth_limit(kind, empty):
    payload = {'note': nested(42, kind, 20), 'duration_ms': 5, 'title': 'ordinary'}
    truncated = dict(payload, note=nested(empty, kind, 19))
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(truncated),))
    actual = boundary.export(payload)
    assert actual == EmissionBoundary(ContentPolicy(True)).export(payload)
    assert leaf(actual['note'], kind, 20) == 42
    assert actual['title'] == 'ordinary' and actual['duration_ms'] == 5


def test_internal_cycle_retains_its_empty_container_in_exact_comparison():
    cycle = []
    cycle.append(cycle)
    payload = {'note': dict(PROTECTED, ignored=cycle)}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(PROTECTED),))
    assert boundary.export(payload) == {'note': {'opaque': PROTECTED['opaque'], 'ignored': []}}


def test_recording_uses_ancestor_context_before_filtering_step_fields(tmp_path):
    steps = [{'name': 'ordinary'}, {'name': 'protected'}]
    steps[1]['steps'] = [dict(PROTECTED, ignored=steps)]
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(PROTECTED),))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    filename = recorder.record_test(result(), steps=steps)
    saved = json.loads((tmp_path / filename).read_text())
    assert saved['steps'][0] == {'name': 'ordinary'}
    assert saved['steps'][1]['name'] == 'protected'
    assert 'owned-context-fragment' not in json.dumps(saved)


def test_recording_uses_depth_context_before_filtering_step_fields(tmp_path):
    node = dict(PROTECTED, ignored={'ordinary': 42})
    steps = [{'name': 'ordinary'}, {'name': 'protected', 'note': nested(node, 'dictionary', 17)}]
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps(dict(PROTECTED, ignored={})),))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    filename = recorder.record_test(result(), steps=steps)
    saved = json.loads((tmp_path / filename).read_text())
    assert saved['steps'][0] == {'name': 'ordinary'}
    assert saved['steps'][1]['name'] == 'protected'
    assert 'owned-context-fragment' not in json.dumps(saved)


@pytest.mark.parametrize('shell', ['', '\x00', '\u200b'])
@pytest.mark.parametrize('cut', [1, 11])
@pytest.mark.parametrize('leading_members', ['', '"ordinary":1,"ordinary":2,'])
def test_original_split_key_verdict_survives_destructive_bearer_masking(shell, cut, leading_members):
    key = shell + '"Bearer \\u006fwned-secondary-value"'
    text = '{' + leading_members + json.dumps({key: 'owned-secondary-value'})[1:]
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('owned-primary',), preserve_json_text=True)
    assert boundary.export({key: 'owned-secondary-value'}) == {}
    assert 'owned-secondary-value' not in json.dumps(boundary.export({'text': text}))
    actual = boundary.export({'parts': [text[:cut], text[cut:]]})
    assert 'owned-secondary-value' not in json.dumps(actual)


def test_per_part_masking_cannot_create_another_registered_credential():
    omitted_number = '9007199254740992'
    boundary = EmissionBoundary(ContentPolicy(True),
        secrets=('owned-replaced-value', 'before' + omitted_number + '[REDACTED]after'))
    actual = boundary.export({'parts': ['before', omitted_number, 'owned-replaced-value', 'after']})
    assert actual == {'parts': ['[REDACTED]']}
