"""Original keys and accepted native containers share credential semantics."""
import json
import sys
import time

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.diagnostics import CredentialScrubber
from api_agent.runtime import prerequisites
from api_agent.testing.recording import RunRecorder
from test_recording import identity, result
from test_terminal_failure_diagnostics import terminal_failure_launch


@pytest.mark.parametrize('layers', [0, 1, 2])
@pytest.mark.parametrize('secret,key', [
    ('owned-key-credential', r'"owned-key-\u0063redential"'),
    ('ordinary-primary', r'"Bearer owned-secondary"'),
    ('{"omega":9}', '{"ome\\u0067a":9}'),
])
def test_recursive_encoded_native_keys_are_omitted_before_insertion(layers, secret, key):
    for _ in range(layers):
        key = json.dumps(key)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({key: 'ordinary', 'ordinary': 'retained'}) == {'ordinary': 'retained'}
    assert boundary.export({'note': {key: 'ordinary', 'ordinary': 'retained'}}) == {'note': {'ordinary': 'retained'}}


@pytest.mark.parametrize('key', ['ordinary', '"ordinary"', '["ordinary"]', '{"ordinary":9}'])
def test_ordinary_encoded_keys_retain_original_spelling(key):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('owned-key-credential',))
    assert boundary.export({key: 'retained'}) == {key: 'retained'}


@pytest.mark.parametrize('shell', ['\x00', '\u200b'])
@pytest.mark.parametrize('at_start', [False, True])
@pytest.mark.parametrize('key_kind', ['registered', 'credential_role'])
def test_invisible_shell_cannot_conceal_encoded_credential_key(shell, at_start, key_kind):
    credential = 'owned-invisible-key-secret'
    key = json.dumps(credential if key_kind == 'registered' else 'password')
    wrapped = shell + key if at_start else key + shell
    value = 'owned-secondary-value'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(credential,))
    actual = boundary.export({'note': {wrapped: value, 'ordinary': 'retained'}})
    assert actual == {'note': {'ordinary': 'retained'}}


@pytest.mark.parametrize('shell', ['\x00', '\u200b'])
def test_invisible_shell_on_ordinary_encoded_key_preserves_spelling(shell):
    key = shell + json.dumps('ordinary')
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('owned-secret',))
    assert boundary.export({'note': {key: 'retained'}}) == {'note': {key: 'retained'}}


@pytest.mark.parametrize('invalid', [
    (b'unsupported', 'value'),
    ('unsupported', object()),
    ('unsupported', 1 << 53),
    ('unsupported', float('inf')),
])
@pytest.mark.parametrize('carrier', ['dict', 'list', 'tuple'])
def test_invalid_member_does_not_fragment_registered_container(invalid, carrier):
    protected = {'password': 'owned-primary', 'opaque': 'owned-secondary'}
    protected[invalid[0]] = invalid[1]
    value = protected if carrier == 'dict' else [protected] if carrier == 'list' else (protected,)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(json.dumps({'password': 'owned-primary', 'opaque': 'owned-secondary'}),))
    actual = boundary.export({'note': value, 'ordinary': 'retained'})
    assert actual['ordinary'] == 'retained'
    assert 'owned-secondary' not in json.dumps(actual)


def test_admissible_extra_member_does_not_match_registered_subset():
    registered = json.dumps({'password': 'owned-primary', 'opaque': 'owned-secondary'})
    value = {'password': 'owned-primary', 'opaque': 'owned-secondary', 'extra': 'ordinary'}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(registered,))
    assert boundary.export({'note': value}) == {'note': {'opaque': 'owned-secondary', 'extra': 'ordinary'}}


def test_recorded_step_omits_fragmented_registered_container(tmp_path):
    registered = json.dumps({'password': 'owned-primary', 'opaque': 'owned-secondary'})
    protected = {'password': 'owned-primary', 'opaque': 'owned-secondary', b'invalid': 'ordinary'}
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=EmissionBoundary(ContentPolicy(True), secrets=(registered,)))
    filename = recorder.record_test(result(), steps=({'name': 'safe'}, {'name': 'protected', 'steps': (protected,)}))
    saved = json.loads((tmp_path / filename).read_text())
    assert saved['steps'][0] == {'name': 'safe'}
    assert 'owned-secondary' not in json.dumps(saved)


@pytest.mark.parametrize('control', ['\x00', '\t', '\u200b'])
def test_tuple_registered_container_is_checked_before_sensitive_member_filtering(control):
    secret = json.dumps([{'password': 'owned-primary', 'ordinary': 'owned-sensitive'}])
    value = ({'pass' + control + 'word': 'owned-primary', 'ordinary': 'owned-sensitive'},)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'note': value, 'status': 'ok'}) == {'status': 'ok'}


@pytest.mark.parametrize('control', ['\x00', '\t', '\u200b'])
def test_actual_recorder_omits_protected_tuple_steps(control, tmp_path):
    secret = json.dumps([{'password': 'owned-primary', 'ordinary': 'owned-sensitive'}])
    steps = ({'pass' + control + 'word': 'owned-primary', 'ordinary': 'owned-sensitive'},)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    filename = recorder.record_test(result(), steps=steps)
    row = json.loads((tmp_path / filename).read_text())
    assert row['steps'] == []
    assert row['status'] == 'passed'
    assert 'owned-sensitive' not in json.dumps(row)


def test_ordinary_tuple_steps_retain_content_in_actual_recorder(tmp_path):
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=EmissionBoundary(ContentPolicy(True), secrets=('true',)))
    filename = recorder.record_test(result(), steps=({'name': 'ordinary', 'status': 'passed'},))
    assert json.loads((tmp_path / filename).read_text())['steps'] == [{'name': 'ordinary', 'status': 'passed'}]


def test_tuple_list_cycle_retains_safe_siblings():
    cycle = []
    value = (cycle,)
    cycle.append(value)
    payload = {'status': 'ok', 'note': {'ordinary': 'retained', 'cycle': value}}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    assert boundary.export(payload) == EmissionBoundary(ContentPolicy(True)).export(payload)


def test_tuple_depth_matches_array_depth_without_erasing_safe_siblings():
    from api_agent.telemetry.redaction import JSONDepthError, normalized_json
    value = 'ordinary'
    for _ in range(24):
        value = (value,)
    with pytest.raises(JSONDepthError):
        normalized_json(value)
    payload = {'status': 'ok', 'note': {'ordinary': 'retained', 'deep': value}}
    assert EmissionBoundary(ContentPolicy(True), secrets=('true',)).export(payload) == EmissionBoundary(ContentPolicy(True)).export(payload)


class UnsupportedKey:
    def __iter__(self):
        raise AssertionError('Unsupported keys must not be iterated')
    def __str__(self):
        raise AssertionError('Unsupported keys must not be stringified')


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('key', [b'ordinary', (17,), frozenset((17,)), UnsupportedKey()])
def test_unsupported_key_types_are_omitted_without_invoking_methods(enabled, key):
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('true',))
    assert boundary.export({'status': 'ok', key: 'ignored'}) == {'status': 'ok'}


@pytest.mark.parametrize('enabled', [False, True])
def test_oversized_integer_only_omits_its_unsupported_member(enabled):
    payload = {'status': 'ok', 'duration_ms': 7, 'too_large': 10**5000}
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('true',))
    assert boundary.export(payload) == {'status': 'ok', 'duration_ms': 7}


@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
def test_malformed_utf8_tail_cannot_hide_recoverable_registered_prefix(tail):
    secret = 'owned-€-😀-credential-remaining'
    captured = 'ordinary / owned-€'.encode() + tail
    assert CredentialScrubber({'API_KEY': secret}).capture(captured) == 'ordinary / [REDACTED]'


@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
def test_unrelated_malformed_utf8_tail_retains_normal_decode_policy(tail):
    captured = b'ordinary' + tail
    assert CredentialScrubber({'API_KEY': 'owned-credential'}).capture(captured) == captured.decode('utf-8', errors='replace')


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
def test_actual_failed_helper_masks_prefix_before_diagnostic_export(stream, tail, tmp_path):
    secret = 'owned-€-😀-credential-remaining'
    captured = 'ordinary / owned-€'.encode() + tail
    helper = tmp_path / 'owned_writer.py'
    helper.write_text('import os,sys\nos.write(int(sys.argv[1]),bytes.fromhex(sys.argv[2]))\nsys.exit(7)\n')
    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream), captured.hex()),
            environment={'API_KEY': secret}, directory=tmp_path, deadline=time.monotonic()+5, cancelled=lambda: False)
    assert 'owned-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
@pytest.mark.parametrize('termination', ['cancel', 'deadline', 'cleanup'])
def test_interrupted_helper_masks_malformed_tail_and_completes_disposal(stream, tail, termination, tmp_path, monkeypatch):
    captured = 'ordinary / owned-€'.encode() + tail
    helper = tmp_path / 'owned_interrupted_writer.py'
    helper.write_text('import os,sys,time\nos.write(int(sys.argv[1]),bytes.fromhex(sys.argv[2]))\ntime.sleep(0 if sys.argv[3]=="cleanup" else 30)\nsys.exit(7)\n')
    observed, finalized = [], []
    original_read, original_finalize = prerequisites.os.read, prerequisites.finalize

    def read(fd, size):
        data = original_read(fd, size)
        observed.append(data)
        return data

    def finalize(process):
        finalized.append(process)
        original_finalize(process)
        if termination == 'cleanup':
            raise RuntimeError('owned disposal reporting failure')

    monkeypatch.setattr(prerequisites.os, 'read', read)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    now = lambda: 2 if any(observed) and termination == 'deadline' else 0
    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream), captured.hex(), termination),
            environment={'API_KEY': 'owned-€-😀-credential-remaining'}, directory=tmp_path, deadline=1,
            cancelled=lambda: any(observed) and termination == 'cancel', now=now)
    assert 'owned-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_recorder_omits_unsupported_step_keys(enabled, tmp_path):
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('true',))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    filename = recorder.record_test(result(), steps=({'name': 'ordinary', b'unsupported': 'ignored'},))
    row = json.loads((tmp_path / filename).read_text())
    assert row['steps'] == [{'name': 'ordinary'}]
    assert row['status'] == 'passed'


@pytest.mark.parametrize('key', ['{ordinary', '"unfinished'])
def test_malformed_ordinary_encoded_keys_retain_original_spelling(key):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    assert boundary.export({key: 'retained'}) == {key: 'retained'}


@pytest.mark.parametrize('key', ['{"omega":9,"omega":3}', pytest.param('[' * 10000 + '0' + ']' * 10000, id='oversized-key')])
def test_ambiguous_or_unbounded_encoded_key_is_omitted(key):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    assert boundary.export({key: 'ignored', 'duration_ms': 7}) == {'duration_ms': 7}


@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
def test_successful_helper_retains_functional_stdout_with_malformed_tail(tail, tmp_path):
    captured = 'owned-€'.encode() + tail
    helper = tmp_path / 'owned_success_writer.py'
    helper.write_text('import os,sys\nos.write(1,bytes.fromhex(sys.argv[1]))\n')
    actual = prerequisites.run_command((sys.executable, str(helper), captured.hex()), environment={'API_KEY': 'owned-€-😀-credential'},
        directory=tmp_path, deadline=time.monotonic()+5, cancelled=lambda: False)
    assert actual == captured.decode('utf-8', errors='replace')


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82'])
def test_malformed_tail_prefix_at_actual_response_bound(stream, tail, tmp_path, monkeypatch):
    helper = tmp_path / 'owned_bounded_writer.py'
    helper.write_text('import os,sys\nfd=int(sys.argv[1])\nprefix=bytes.fromhex(sys.argv[2])\ntail=bytes.fromhex(sys.argv[3])\ndata=b"x"*(8*1024*1024+1-len(prefix)-len(tail))+prefix+tail\nwhile data:\n count=os.write(fd,data)\n data=data[count:]\n')
    original_read = prerequisites.os.read
    monkeypatch.setattr(prerequisites.os, 'read', lambda fd, size: original_read(fd, min(size, 127)))
    finalized = []
    original_finalize = prerequisites.finalize

    def finalize(process):
        finalized.append(process)
        original_finalize(process)

    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    with pytest.raises(prerequisites.PrerequisiteError, match='safe bound') as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream), 'owned-€'.encode().hex(), tail.hex()),
            environment={'API_KEY': 'owned-€-😀-credential-remaining'}, directory=tmp_path,
            deadline=time.monotonic()+20, cancelled=lambda: False)
    assert 'owned-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


@pytest.mark.parametrize('layers', [1, 2, 3])
@pytest.mark.parametrize('key', ['password', 'p\x00assword', 'authorization'])
def test_recursively_encoded_credential_property_names_are_classified_before_values(layers, key):
    for _ in range(layers):
        key = json.dumps(key)
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({'note': {key: 'owned-unknown-secondary', 'ordinary': 'retained'}}) == {'note': {'ordinary': 'retained'}}


@pytest.mark.parametrize('key', ['input_tokens', 'ordinary_role', 'ordinary_path'])
def test_encoded_ordinary_property_names_keep_original_spelling(key):
    encoded = json.dumps(json.dumps(key))
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({encoded: 'retained'}) == {encoded: 'retained'}


def test_recursive_ordinary_key_depth_is_bounded_before_member_insertion():
    key = 'ordinary'
    for _ in range(22):
        key = json.dumps(key)
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({key: 'ignored', 'duration_ms': 7}) == {'duration_ms': 7}


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('tail', [b'\xff', b'\xe2\x82', b'\xed\xa0\x80'])
def test_terminal_bootstrap_keeps_content_off_malformed_prefix_private(terminal_failure_launch, stream, tail, tmp_path, monkeypatch, capsys):
    from api_agent.runtime.bootstrap import main
    from test_terminal_failure_diagnostics import ARGS, ROOT
    captured = 'ordinary / owned-€'.encode() + tail
    helper = tmp_path / 'owned_terminal_writer.py'
    helper.write_text('import os,sys\nos.write(int(sys.argv[1]),bytes.fromhex(sys.argv[2]))\nsys.exit(7)\n')
    finalized = []
    original_finalize = prerequisites.finalize

    def finalize(process):
        finalized.append(process)
        original_finalize(process)

    def fail_integration(context, selected, config, **options):
        assert config['LOG_CONTENT'] is False
        prerequisites.run_command((sys.executable, str(helper), str(stream), captured.hex()),
            environment={'API_KEY': 'owned-€-😀-credential-remaining'}, directory=tmp_path,
            deadline=time.monotonic()+5, cancelled=lambda: False)

    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', fail_integration)
    assert main(ARGS, base_root=ROOT) == 1
    output = capsys.readouterr().err
    assert 'owned-' not in output
    assert '[REDACTED]' in output
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


class UnsupportedList(list):
    def __iter__(self):
        raise AssertionError('Unsupported list must not be iterated')


class UnsupportedDict(dict):
    def items(self):
        raise AssertionError('Unsupported dictionary methods must not run')


class UnsupportedTuple(tuple):
    def __iter__(self):
        raise AssertionError('Unsupported tuple must not be iterated')


class UnsupportedString(str):
    def __iter__(self):
        raise AssertionError('Unsupported string must not be iterated')
    def __str__(self):
        raise AssertionError('Unsupported string must not be converted')


class UnsupportedInteger(int):
    def __str__(self):
        raise AssertionError('Unsupported integer must not be converted')


class UnsupportedFloat(float):
    def __float__(self):
        raise AssertionError('Unsupported float must not be converted')


UNSUPPORTED_VALUES = [UnsupportedList([1]), UnsupportedDict(ordinary=1), UnsupportedTuple((1,)),
                      UnsupportedString('ordinary'), UnsupportedInteger(1), UnsupportedFloat(1.5)]


@pytest.mark.parametrize('value', UNSUPPORTED_VALUES, ids=['list', 'dict', 'tuple', 'str', 'int', 'float'])
@pytest.mark.parametrize('enabled', [False, True])
def test_unsupported_value_subclasses_are_omitted_without_invoking_methods(value, enabled):
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('true',))
    assert boundary.export({'outer': value, 'duration_ms': 8}) == {'duration_ms': 8}


@pytest.mark.parametrize('value', UNSUPPORTED_VALUES, ids=['list', 'dict', 'tuple', 'str', 'int', 'float'])
def test_native_normalizer_rejects_unsupported_subclasses_before_encoding(value):
    from api_agent.telemetry.redaction import normalized_json
    with pytest.raises(TypeError, match='exact native types'):
        normalized_json(value)


@pytest.mark.parametrize('value', UNSUPPORTED_VALUES, ids=['list', 'dict', 'tuple', 'str', 'int', 'float'])
def test_actual_recorder_omits_unsupported_step_values(value, tmp_path):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    filename = recorder.record_test(result(), steps=({'name': 'ordinary', 'unsupported': value},))
    assert json.loads((tmp_path / filename).read_text())['steps'] == [{'name': 'ordinary'}]
