"""Serialized text partitions use the same credential policy as whole text."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from test_partitioned_credentials import invoke, TOKEN, REQUEST, SECONDARY
from test_responses_native_metadata import native
from api_agent.providers.inference import request_to_wire, result_from_wire
from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer, _Peer, _decode
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.inference_content import content_boundary, export_content, content_attachment
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult


def encoded_parts(credential, layers=1, shape="string"):
    encoded = '"Be\\u0061rer \\u' + format(ord(credential[0]), '04x') + credential[1:] + '"'
    if shape == 'object':
        encoded = '{"note":' + encoded + '}'
    elif shape == 'array':
        encoded = '[' + encoded + ']'
    elif shape == 'nested':
        encoded = '{"outer":[{"note":' + encoded + '}]}'
    elif shape in ('credential-key', 'credential-key-trailing'):
        encoded = '{"access_token":"\\u' + format(ord(credential[0]), '04x') + credential[1:] + '"}'
        if shape == 'credential-key-trailing':
            encoded += '.'
    for _ in range(layers - 1):
        encoded = json.dumps(encoded)
    return (encoded[:10], encoded[10:])


@pytest.mark.parametrize('credential', [TOKEN, SECONDARY])
@pytest.mark.parametrize('layers', [1, 2, 3])
@pytest.mark.parametrize('normalized', [False, True])
def test_joined_serialized_credentials_are_redacted_recursively(credential, layers, normalized):
    parts = list(encoded_parts(credential, layers))
    if normalized:
        parts.insert(1, '9007199254740992')
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.export({'nested': {'parts': parts}}) == {'nested': {'parts': ['[REDACTED]']}}
    assert boundary.content_export({'parts': parts}) == {'parts': ['[REDACTED]']}


@pytest.mark.parametrize('parts', [('"or\\u0064', 'inary text"'), ('ordinary ', 'text'), ('{"number":', ' 42}'), ('[1,', '2]')])
def test_ordinary_serialized_partitions_remain_exact(parts):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.export({'parts': parts}) == {'parts': list(parts)}
    assert boundary.content_export({'parts': parts}) == {'parts': list(parts)}
    assert EmissionBoundary(secrets=(TOKEN,)).export({'parts': parts}) == {}


@pytest.mark.parametrize('shape', ['string', 'object', 'array', 'nested', 'credential-key', 'credential-key-trailing'])
@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'http', 'sse'])
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('credential', [TOKEN, SECONDARY, 'ordinary text'])
def test_serialized_partition_ipc_frames_and_recording(shape, subscription, enabled, credential, tmp_path):
    if credential == 'ordinary text':
        ordinary = '"or\\u0064inary text"'
        containers = {'string': ordinary, 'object': '{"note":' + ordinary + '}',
                      'array': '[' + ordinary + ']', 'nested': '{"outer":[{"note":' + ordinary + '}]}',
                      'credential-key': '{"note":' + ordinary + '}',
                      'credential-key-trailing': '{"note":' + ordinary + '}.'}
        encoded = containers[shape]
        parts = (encoded[:10], encoded[10:])
    else:
        parts = encoded_parts(credential, shape=shape)

    async def scenario():
        constructed = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                       (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
        execution_results = []
        async def complete(request):
            if subscription is None:
                execution_results.append(constructed)
                return constructed
            data = native([], 'commentary')
            data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[]) for part in parts]
            result = await invoke(data, subscription, request)
            execution_results.append(result)
            return result
        backend = content_boundary({'LOG_CONTENT': enabled, 'API_KEY': TOKEN})
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-json-capability-0000000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            result = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            actual = result_from_wire(result['result'])
            assert actual == execution_results[0]
            assert tuple(part.text for part in actual.payload.content) == parts
            assert actual.usage == Usage(7, 3)
            evidence = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            if enabled:
                response = next(row for row in evidence if row['phase'] == 'response')
                expected = list(parts) if credential == 'ordinary text' else ['[REDACTED]']
                # Assert serialized backend frames before any parent compensation.
                assert response['content']['message']['content'] == expected
            else:
                assert evidence == []
            parent = EmissionBoundary(ContentPolicy(enabled))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(phase=row["phase"], content=row["content"], call_id=str(uuid4())))), parent) for row in evidence]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            identity = TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned')
            name = recorder.record_test(TestResult(identity, 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_bytes())
            if enabled:
                attachment = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                recorded = json.loads((tmp_path / attachment['source']).read_bytes())
                assert recorded['message']['content'] == expected
            else:
                assert saved['attachments'] == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('key', ['\u200b' + json.dumps('password'),
                                '\x00"Bearer \\u006fwned-secondary-credential"',
                                '"Bearer \\u006fwned-secondary-credential"'])
@pytest.mark.parametrize('cut', [1, 11])
@pytest.mark.parametrize('leading_members', ['', '"ordinary":1,"ordinary":2,'])
def test_invisible_encoded_key_is_private_in_worker_frame_and_recording(key, cut, leading_members, tmp_path):
    text = '{' + leading_members + json.dumps({key: 'owned-secondary-credential', 'ordinary': 'safe'})[1:]
    parts = (text[:cut], text[cut:])

    async def scenario():
        result = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                   (ToolCall('call-owned', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
        backend = content_boundary({'LOG_CONTENT': True, 'API_KEY': 'owned-primary-credential'})
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=AsyncMock(return_value=result),
                                aclose=AsyncMock(), export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-json-capability-0000000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            reply = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            assert result_from_wire(reply['result']) == result
            evidence = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            response = next(row for row in evidence if row['phase'] == 'response')
            assert 'owned-secondary-credential' not in json.dumps(response)
            parent = EmissionBoundary(ContentPolicy(True))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(
                call_id=str(uuid4()), phase=row['phase'], content=row['content']))), parent) for row in evidence]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            test = TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned')
            filename = recorder.record_test(TestResult(test, 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / filename).read_text())
            assert 'owned-secondary-credential' not in json.dumps(saved)
            for attachment in saved['attachments']:
                assert 'owned-secondary-credential' not in (tmp_path / attachment['source']).read_text()
        finally:
            await server.aclose()
            await registry.aclose()

    asyncio.run(scenario())


def test_serialized_text_depth_and_recursion_fail_closed(monkeypatch):
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary._decoded_text_credential('ordinary', 21)
    def recursive(value, **kwargs):
        raise RecursionError
    monkeypatch.setattr('api_agent.telemetry.privacy.json.loads', recursive)
    assert boundary.export({'parts': ['ordinary', ' text']}) == {'parts': ['[REDACTED]']}


def test_raw_decoded_registered_text_checked_before_part_omission():
    secret = 'synthetic-9007199254740992-auth'
    parts = ('"\\u0073ynthetic-', '9007199254740992', '-auth"')
    assert json.loads(''.join(parts)) == secret
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'parts': parts}) == {'parts': ['[REDACTED]']}


@pytest.mark.parametrize('encoded', ['null', 'true', 'false', '42', '1.25', '{"ordinary":[null,true,false,42,1.25]}', '[]', '{}'])
def test_ordinary_combined_json_scalars_and_containers(encoded):
    parts = (encoded[:2], encoded[2:])
    assert EmissionBoundary(ContentPolicy(True)).content_export({'parts': parts}) == {'parts': list(parts)}


@pytest.mark.parametrize('parts', [
    ('["Be",', '"arer synthetic-secondary-auth"]'),
    ('["Be","9007199254740992",', '"arer synthetic-secondary-auth"]'),
    ('{"ordinary":["Be","9007199254740992",', '"arer synthetic-secondary-auth"]}'),
    ('{"Be\\u0061r', 'er \\u0073ynthetic-secondary-auth":"ordinary"}'),
    ('{"access_', 'token":"ordinary"}'),
    ('[{"access_', 'token":"ordinary"}]'),
    ('["Bearer synthetic-secondary-auth",', '"ordinary"]'),
])
def test_credentials_in_combined_json_semantics(parts):
    assert EmissionBoundary(ContentPolicy(True)).content_export({'parts': parts}) == {'parts': ['[REDACTED]']}


@pytest.mark.parametrize('encoded, secret', [('123', '123'), ('true', 'True'), ('null', 'None')])
def test_decoded_scalar_registered_credential(encoded, secret):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary._decoded_text_credential(encoded, 0)


def test_decoded_container_depth_is_bounded():
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary._decoded_value_credential({'ordinary': 'text'}, 21)


@pytest.mark.parametrize('prefix, suffix', [('{"note":"', '"}'), ('["', '"]'), ('{"outer":["', '"]}')])
def test_raw_decoded_container_checked_before_part_omission(prefix, suffix):
    secret = 'synthetic-9007199254740992-auth'
    parts = (prefix + '\\u0073ynthetic-', '9007199254740992', '-auth' + suffix)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'parts': parts}) == {'parts': ['[REDACTED]']}


@pytest.mark.parametrize('prefix, suffix', [('{"note":"', '"}'), ('["', '"]'), ('{"outer":["', '"]}')])
def test_normalized_decoded_container_after_part_omission(prefix, suffix):
    parts = (prefix + 'Be\\u0061r', '9007199254740992', 'er \\u0073ynthetic-secondary-auth' + suffix)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.export({'parts': parts}) == {'parts': ['[REDACTED]']}


REGISTERED_REPRESENTATIONS = [
    ('{"a":1}', '{"payload":{"\\u0061":1}}'),
    ('["synthetic-json-auth"]', '{"payload":[ "\\u0073ynthetic-json-auth" ]}'),
    ('"synthetic-json-auth"', '{"note":"\\u0073ynthetic-json-auth"}'),
    ('{"n":42}', '{"payload":{"\\u006e":42}}'),
    ('42', '{"note":"\\u0034\\u0032"}'),
    ('true', '{"note":"\\u0074rue"}'),
]


CONTROL_INSERTION_REPRESENTATIONS = [
    (TOKEN, json.dumps(container(TOKEN[:9] + control + TOKEN[9:])))
    for control in ('\x00', '\t', '\u200b')
    for container in (lambda text: text, lambda text: {'note': text}, lambda text: [text])
]


JSON_SHAPE_CONTROL_REPRESENTATIONS = [
    (json.dumps({'omega': 9}, separators=(',', ':')), json.dumps({'payload': {'ome' + control + 'ga': 9}}))
    for control in ('\x00', '\t', '\u200b')
] + [
    (json.dumps(['synthetic-owned-json']), json.dumps({'payload': ['synthetic-' + control + 'owned-json']}))
    for control in ('\x00', '\t', '\u200b')
] + [
    (json.dumps('synthetic-owned-json'), json.dumps({'note': 'synthetic-' + control + 'owned-json'}))
    for control in ('\x00', '\t', '\u200b')
]
ENCODED_KEY_REPRESENTATIONS = [
    (TOKEN, json.dumps({key: 'ordinary'}))
    for key in ('"' + TOKEN[:9] + '\\u' + format(ord(TOKEN[9]), '04x') + TOKEN[10:] + '"',
                json.dumps('Bearer synthetic-owned-key-secondary'),
                json.dumps(json.dumps('password')))
]


CONCEALED_SCHEME_REPRESENTATIONS = [
    ('Bearer', 'Be' + control + 'arer synthetic-independent-secondary-auth')
    for control in ('\t', '\n', '\r', '\x85', '\xa0', '\u2028', '\t\u200b')
]


@pytest.mark.parametrize('secret, encoded', REGISTERED_REPRESENTATIONS + CONTROL_INSERTION_REPRESENTATIONS
                         + JSON_SHAPE_CONTROL_REPRESENTATIONS + CONCEALED_SCHEME_REPRESENTATIONS + ENCODED_KEY_REPRESENTATIONS)
@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'json', 'sse'])
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'enabled'])
def test_registered_json_representation_ipc_and_recording(secret, encoded, subscription, enabled, tmp_path, monkeypatch):
    import test_partitioned_credentials
    transport_secret = TOKEN if secret == 'Bearer' else secret
    monkeypatch.setattr(test_partitioned_credentials, 'TOKEN', transport_secret)
    parts = (encoded[:6], encoded[6:])
    if secret == 'Bearer':
        scheme, separator, secondary = encoded.partition(' ')
        parts = (scheme + separator, secondary)

    async def scenario():
        produced = []
        async def complete(request):
            if subscription is None:
                actual = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                           (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
            else:
                data = native([], 'commentary')
                data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[]) for part in parts]
                actual = await invoke(data, subscription, request)
            produced.append(actual)
            return actual
        backend = content_boundary({'LOG_CONTENT': enabled, 'API_KEY': transport_secret, 'PASSWORD': secret})
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-representation-capability-000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            wire = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            actual = result_from_wire(wire['result'])
            assert actual == produced[0]
            assert tuple(part.text for part in actual.payload.content) == parts
            rows = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            if enabled:
                response = next(row for row in rows if row['phase'] == 'response')
                assert response['content']['message']['content'] == ['[REDACTED]']
            else:
                assert rows == []
            parent = EmissionBoundary(ContentPolicy(enabled), secrets=(secret,))
            assert parent.export({'parts': parts}) == ({'parts': ['[REDACTED]']} if enabled else {})
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(phase=row['phase'], content=row['content'], call_id=str(uuid4())))), parent) for row in rows]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            name = recorder.record_test(TestResult(TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned'), 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_bytes())
            if enabled:
                attachment = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                recorded = json.loads((tmp_path / attachment['source']).read_bytes())
                assert recorded['message']['content'] == ['[REDACTED]']
            else:
                assert saved['attachments'] == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('secret, encoded', REGISTERED_REPRESENTATIONS)
@pytest.mark.parametrize('canonical', [False, True])
def test_registered_representation_combined_and_contiguous(secret, encoded, canonical):
    parts = [encoded[:6], encoded[6:]]
    if canonical:
        parts.insert(1, '9007199254740992')
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.content_export({'parts': parts}) == {'parts': ['[REDACTED]']}
    contiguous = boundary.content_export({'parts': [encoded]})
    normalized_secret = json.dumps(json.loads(secret), ensure_ascii=False)
    assert normalized_secret not in ''.join(contiguous['parts'])


def test_combined_canonical_encoding_recursion_fails_closed(monkeypatch):
    boundary = EmissionBoundary(ContentPolicy(True))
    def recursive(value, **kwargs):
        raise RecursionError
    monkeypatch.setattr('api_agent.telemetry.privacy.json.dumps', recursive)
    assert boundary._decoded_text_credential('{"ordinary":"text"}', 0)


@pytest.mark.parametrize('secret, encoded', REGISTERED_REPRESENTATIONS)
def test_registered_representation_after_structural_part_omission(secret, encoded):
    parts = (encoded[:1], '9007199254740992', encoded[1:])
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'parts': parts}) == {'parts': ['[REDACTED]']}


HIDDEN_SCHEME_PARTS = [
    ('Be', '\x00', 'arer ' + SECONDARY),
    ('Be', '\u200b', 'arer ' + SECONDARY),
    ('Bearer', '\u200b', ' ' + SECONDARY),
    ('Be\x00a', 'rer\t\u200b', SECONDARY),
    ('Be\u200da', 'rer\n\ufeff', SECONDARY),
]


@pytest.mark.parametrize('parts, expected', [(parts, ('[REDACTED]',)) for parts in HIDDEN_SCHEME_PARTS] + [
    pytest.param(('\x00Bearer ', '[REDACTED]\u200b'), ('\x00Bearer ', '[REDACTED]\u200b'), id='ordinary-redacted-control'),
    pytest.param(('\x00Bearer ', '[REDACTED]\u200b' + SECONDARY), ('[REDACTED]',), id='actual-credential-suffix'),
])
@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'json', 'sse'])
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'enabled'])
def test_hidden_bearer_scheme_ipc_and_recording(parts, expected, subscription, enabled, tmp_path):
    async def scenario():
        produced = []
        async def complete(request):
            if subscription is None:
                actual = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                           (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
            else:
                data = native([], 'commentary')
                data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[]) for part in parts]
                actual = await invoke(data, subscription, request)
            produced.append(actual)
            return actual
        backend = content_boundary({'LOG_CONTENT': enabled, 'API_KEY': TOKEN})
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-hidden-scheme-capability-000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            wire = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            actual = result_from_wire(wire['result'])
            assert actual == produced[0]
            assert tuple(part.text for part in actual.payload.content) == parts
            rows = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            if enabled:
                response = next(row for row in rows if row['phase'] == 'response')
                # Inspect backend bytes before the parent can scrub them.
                assert response['content']['message']['content'] == list(expected)
            else:
                assert rows == []
            parent = EmissionBoundary(ContentPolicy(enabled))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(phase=row['phase'], content=row['content'], call_id=str(uuid4())))), parent) for row in rows]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            name = recorder.record_test(TestResult(TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned'), 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_bytes())
            if enabled:
                attachment = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                recorded = json.loads((tmp_path / attachment['source']).read_bytes())
                assert recorded['message']['content'] == list(expected)
            else:
                assert saved['attachments'] == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('parts', HIDDEN_SCHEME_PARTS)
@pytest.mark.parametrize('layers', [0, 1, 2])
def test_hidden_scheme_raw_and_decoded_representations(parts, layers):
    source = ''.join(parts)
    for _ in range(layers):
        source = json.dumps({'note': source})
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    output = boundary.content_export({'parts': (source[:2], source[2:])})
    assert SECONDARY not in ''.join(output['parts'])


@pytest.mark.parametrize('text', ['ordinary\x00text', 'Be\u200bavers build', 'Bearer\u200bprose without spacing',
                                  '\x00Bearer [REDACTED] \u200b', 'prefix\x00text\tending', 'Bearer\u200b'])
def test_detection_view_preserves_ordinary_control_text(text):
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary._text(text) == text
    assert boundary.content_export({'parts': [text]}) == {'parts': [text]}


def test_hidden_scheme_redacts_only_credential_spans():
    boundary = EmissionBoundary(ContentPolicy(True))
    source = '\x00ordinary\u200b Be\x00arer synthetic-first; unchanged\x00 Be\u200barer synthetic-second \u200bending'
    expected = '\x00ordinary\u200b Bearer [REDACTED]; unchanged\x00 Bearer [REDACTED] \u200bending'
    assert boundary._text(source) == expected


COLLIDING_CREDENTIALS = [
    ('Bearer', ('Bearer ', SECONDARY)),
    ('Bearer', ('Be', 'arer ' + SECONDARY)),
    ('secret-head Bearer inner-secret secret-tail', ('secret-head Bearer ', 'inner-secret secret-tail')),
    ('secret-head Bearer inner-secret secret-tail', ('ordinaryprefix secret-head Bearer inner-secret secret-tail ordinarysuffix',)),
]


@pytest.mark.parametrize('secret, source_parts', COLLIDING_CREDENTIALS)
@pytest.mark.parametrize('layers', [0, 1, 2])
@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'json', 'sse'])
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'enabled'])
def test_registered_syntax_collisions_ipc_and_recording(secret, source_parts, layers, subscription, enabled, tmp_path):
    if layers:
        encoded = ''.join(source_parts)
        for _ in range(layers):
            encoded = json.dumps({'note': encoded})
        encoded = encoded.replace('Bearer', '\\u0042earer')
        parts = (encoded[:10], encoded[10:])
    else:
        parts = source_parts

    async def scenario():
        produced = []
        async def complete(request):
            if subscription is None:
                actual = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                           (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
            else:
                data = native([], 'commentary')
                data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[]) for part in parts]
                actual = await invoke(data, subscription, request)
            produced.append(actual)
            return actual
        # Register collisions independently at the evidence seam; mocked native
        # inference retains its unrelated credential so rejection cannot mask it.
        backend = EmissionBoundary(ContentPolicy(enabled), secrets=(secret,))
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-collision-capability-000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            wire = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            actual = result_from_wire(wire['result'])
            assert actual == produced[0]
            assert tuple(part.text for part in actual.payload.content) == parts
            rows = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            if enabled:
                response = next(row for row in rows if row['phase'] == 'response')
                content = ''.join(response['content']['message']['content'])
                assert all(fragment not in content for fragment in (SECONDARY, 'inner-secret', 'secret-head', 'secret-tail'))
                assert secret not in content
            else:
                assert rows == []
            parent = EmissionBoundary(ContentPolicy(enabled))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(phase=row['phase'], content=row['content'], call_id=str(uuid4())))), parent) for row in rows]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            name = recorder.record_test(TestResult(TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned'), 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_bytes())
            if enabled:
                attachment = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                recorded = json.loads((tmp_path / attachment['source']).read_bytes())
                content = ''.join(recorded['message']['content'])
                assert all(fragment not in content for fragment in (SECONDARY, 'inner-secret', 'secret-head', 'secret-tail'))
                assert secret not in content
            else:
                assert saved['attachments'] == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('secret, text', [('Bearer', 'Bearer ' + SECONDARY),
    ('secret-head Bearer inner-secret secret-tail', 'secret-head Bearer inner-secret secret-tail')])
def test_original_registered_and_bearer_ranges_are_unioned(secret, text):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary._text(text) == '[REDACTED]'


def test_overlapping_registered_ranges_are_complete():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('aaa',))
    assert boundary._text('aaaa ordinary') == '[REDACTED] ordinary'


def test_registered_marker_collision_does_not_reintroduce_scheme():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('Bearer',))
    assert boundary._text('Be\x00arer ' + SECONDARY) == '[REDACTED]'


def test_registered_scheme_union_at_string_export_seam():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('Bearer',))
    assert boundary.content_export({'note': 'Bearer ' + SECONDARY}) == {'note': '[REDACTED]'}


def test_composite_registered_union_at_string_export_seam():
    secret = 'secret-head Bearer inner-secret secret-tail'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.content_export({'note': 'ordinaryprefix ' + secret + ' ordinarysuffix'}) == {
        'note': 'ordinaryprefix [REDACTED] ordinarysuffix'}


@pytest.mark.parametrize('suffix', ['\u200b', '\u200d', '\ufeff', '\x00', '\u200b\x00'])
def test_redacted_marker_with_nonprinting_suffix_preserves_exact_parts(suffix):
    parts = ('\x00Bearer ', '[REDACTED]' + suffix)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary._text(''.join(parts)) == ''.join(parts)
    assert boundary.content_export({'parts': parts}) == {'parts': list(parts)}
    assert boundary.content_export({'parts': [''.join(parts)]}) == {'parts': [''.join(parts)]}


@pytest.mark.parametrize('suffix', ['\u200b', '\u200d', '\ufeff', '\x00'])
def test_real_credential_after_redacted_marker_nonprinting_suffix_is_removed(suffix):
    parts = ('\x00Bearer ', '[REDACTED]' + suffix + 'synthetic-actual-secret')
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary._text(''.join(parts)) == '\x00Bearer [REDACTED]'
    assert boundary.content_export({'parts': parts}) == {'parts': ['[REDACTED]']}


def test_literal_word_boundary_outside_printable_view_remains_protected():
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary._text('ordinary\x00Bearer ' + SECONDARY) == 'ordinary\x00Bearer [REDACTED]'


def test_registered_redacted_looking_suffix_still_receives_original_range_protection():
    secret = '[REDACTED]\u200b'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary._text('Bearer ' + secret) == 'Bearer [REDACTED]'


@pytest.mark.parametrize('shape', ['string', 'object', 'array', 'nested'])
@pytest.mark.parametrize('layers', [1, 2])
@pytest.mark.parametrize('suffix', ['\u200b', '\x00'])
def test_serialized_redaction_marker_ordinary_all_partitions(shape, layers, suffix):
    marker = 'Bearer [REDACTED]' + suffix
    value = {'string': marker, 'object': {'note': marker}, 'array': [marker],
             'nested': {'outer': [{'note': marker}]}}[shape]
    encoded = json.dumps(value)
    for _ in range(layers - 1):
        encoded = json.dumps(encoded)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.content_export({'parts': [encoded]}) == {'parts': [encoded]}
    for cut in range(len(encoded) + 1):
        parts = [encoded[:cut], encoded[cut:]]
        assert boundary.content_export({'parts': parts}) == {'parts': parts}


@pytest.mark.parametrize('shape', ['string', 'object', 'array', 'nested'])
@pytest.mark.parametrize('layers', [1, 2])
def test_serialized_redaction_marker_actual_suffix_all_partitions(shape, layers):
    marker = 'Bearer [REDACTED]\u200bsynthetic-actual-secret'
    value = {'string': marker, 'object': {'note': marker}, 'array': [marker],
             'nested': {'outer': [{'note': marker}]}}[shape]
    encoded = json.dumps(value)
    for _ in range(layers - 1):
        encoded = json.dumps(encoded)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    for cut in range(len(encoded) + 1):
        parts = [encoded[:cut], encoded[cut:]]
        result = boundary.content_export({'parts': parts})
        assert 'synthetic-actual-secret' not in ''.join(result['parts'])


@pytest.mark.parametrize('secret, encoded', [
    (TOKEN, '{"note":"Be\\u0061rer \\u0073' + TOKEN[1:] + '","note":"ordinary"}'),
    (None, '{"note":"Bearer ' + SECONDARY + '","note":"ordinary"}'),
    ('{"a":1,"b":2}', '{"payload":{"\\u0061":1,"b":2},"payload":"ordinary"}'),
])
def test_shadowed_json_members_cannot_hide_credentials(secret, encoded):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=() if secret is None else (secret,))
    for cut in range(len(encoded) + 1):
        parts = [encoded[:cut], encoded[cut:]]
        assert boundary.content_export({'parts': parts}) == {'parts': ['[REDACTED]']}


def test_shadowed_ordinary_json_fails_closed():
    encoded = json.dumps({'note': 'Bearer [REDACTED]\u200b'})[:-1] + ',"note":"ordinary"}'
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.content_export({'parts': [encoded[:10], encoded[10:]]}) == {'parts': ['[REDACTED]']}
    assert boundary.content_export({'note': encoded}) == {'note': '[REDACTED]'}


DUPLICATE_IPC_CASES = [
    pytest.param(TOKEN, '{"note":"B\\u0065arer \\u0073' + TOKEN[1:] + '","note":"safe"}', True, id='registered-first'),
    pytest.param(None, '{"note":"B\\u0065arer ' + SECONDARY + '","note":"safe"}', True, id='secondary-first'),
    pytest.param(None, '{"note":"safe","no\\u0074e":"Be\\u0061rer ' + SECONDARY + '"}', True, id='secondary-last-escaped-key'),
    pytest.param('{"a":1,"b":2}', '{"payload":{"\\u0061":1,"b":2},"payload":"safe"}', True, id='registered-object-first'),
    pytest.param(None, '{"outer":[{"note":"Be\\u0061rer ' + SECONDARY + '","note":"safe"}]}', True, id='nested-secondary'),
    pytest.param(None, '{"note":"Bearer [REDACTED]\\u200b","note":"safe"}', True, id='ambiguous-marker'),
    pytest.param(None, '{"note":"Bearer [REDACTED]\\u200b"}', False, id='ordinary-marker'),
    pytest.param(None, '{"note":"or\\u0064inary"}', False, id='ordinary-json'),
]


@pytest.mark.parametrize('secret, encoded, ambiguous', DUPLICATE_IPC_CASES)
@pytest.mark.parametrize('partition', ['contiguous', 'split'])
@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'json', 'sse'])
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'enabled'])
def test_duplicate_json_ipc_and_recording(secret, encoded, ambiguous, partition, subscription, enabled, tmp_path, monkeypatch):
    import test_partitioned_credentials
    monkeypatch.setattr(test_partitioned_credentials, 'TOKEN', secret or TOKEN)
    parts = (encoded,) if partition == 'contiguous' else (encoded[:17], encoded[17:])
    expected = ['[REDACTED]'] if ambiguous else list(parts)

    async def scenario():
        produced = []
        async def complete(request):
            if subscription is None:
                actual = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                           (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
            else:
                data = native([], 'commentary')
                data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[]) for part in parts]
                actual = await invoke(data, subscription, request)
            produced.append(actual)
            return actual
        backend = EmissionBoundary(ContentPolicy(enabled), secrets=() if secret is None else (secret,), preserve_json_text=True)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-duplicate-capability-000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            wire = await server._dispatch(peer, {'binding': registration['binding'], 'request': request_to_wire(REQUEST)}, 'complete')
            actual = result_from_wire(wire['result'])
            assert actual == produced[0]
            assert tuple(part.text for part in actual.payload.content) == parts
            rows = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            if enabled:
                response = next(row for row in rows if row['phase'] == 'response')
                assert response['content']['message']['content'] == expected
            else:
                assert rows == []
            parent = EmissionBoundary(ContentPolicy(enabled))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(phase=row['phase'], content=row['content'], call_id=str(uuid4())))), parent) for row in rows]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            name = recorder.record_test(TestResult(TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned'), 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_bytes())
            if enabled:
                attachment = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                recorded = json.loads((tmp_path / attachment['source']).read_bytes())
                assert recorded['message']['content'] == expected
            else:
                assert saved['attachments'] == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('secret, encoded, ambiguous', DUPLICATE_IPC_CASES)
def test_duplicate_json_scalar_export_matches_partition_policy(secret, encoded, ambiguous):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=() if secret is None else (secret,))
    expected = '[REDACTED]' if ambiguous else encoded
    assert boundary.content_export({'note': encoded}) == {'note': expected}


def test_dense_overlapping_registered_range_preserves_all_ordinary_controls():
    secret = 'a' * 40000
    source = 'a' * 80000 + '\u200bordinary\x00' + 'a' * 80000
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary._text(source) == '[REDACTED]\u200bordinary\x00[REDACTED]'


def test_periodic_overlapping_registered_range_preserves_edges():
    secret = 'abab' * 1000
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    source = '\x00before ' + 'abab' * 4000 + ' after\u200b'
    assert boundary._text(source) == '\x00before [REDACTED] after\u200b'
