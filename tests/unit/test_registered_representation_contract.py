"""Credential views share normalization and retain original source ranges."""
import json
import sys
import time

import pytest

from api_agent.runtime import prerequisites
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.diagnostics import CredentialScrubber, _RegisteredRanges
from test_terminal_failure_diagnostics import terminal_failure_launch


@pytest.mark.parametrize('control, escape', [('\t', r'\u0009'), ('\u200b', r'\u200B'), ('\x00', r'\u0000')])
@pytest.mark.parametrize('layers', [0, 1, 2, 3])
@pytest.mark.parametrize('partial', [False, True])
def test_genuine_control_registration_accepts_alternate_carriers(control, escape, layers, partial):
    secret = 'owned-' + control + 'credential-remaining'
    suffix = 'credential-rem' if partial else 'credential-remaining END'
    carrier = 'owned-' + escape + suffix
    for _ in range(layers):
        carrier = json.dumps(carrier)

    result = CredentialScrubber({'API_KEY': secret}).capture(carrier.encode())

    assert 'owned-' not in result
    assert 'credential-' not in result
    assert '[REDACTED]' in result


@pytest.mark.parametrize('secret, carrier', [
    ('owned prefix password=owned-primary suffix-owned', r'o\u0077ned prefix password=owned-primary suffix-owned END'),
    ('https://owned-user:owned-primary@public.invalid/suffix-owned', r'https://owned-user:owned-primary@public.invalid/suffix-\u006fwned END'),
])
def test_encoded_composite_original_range_survives_syntax_edits(secret, carrier):
    assert CredentialScrubber({'API_KEY': secret})(carrier) == '[REDACTED] END'


@pytest.mark.parametrize('secret, carrier', [
    (json.dumps({'omega': 9}), '{"omega": 9} END'),
    (json.dumps({'omega': 9, 'count': 3}), '{"count":3,"ome\\u200bga":9} END'),
    (json.dumps(['owned-json']), '[ "owned-\\u200Bjson" ] END'),
    (json.dumps('owned-json'), '"owned-\\u0000json" END'),
])
def test_json_shaped_registration_protects_semantic_diagnostic_carrier(secret, carrier):
    assert CredentialScrubber({'API_KEY': secret})(carrier) == '[REDACTED] END'


COLLIDING_JSON = '{"omega":9,"ome\\u200bga":"ordinary"}'


@pytest.mark.parametrize('value,expected', [
    (COLLIDING_JSON, '[REDACTED]'),
    ([COLLIDING_JSON[:8], COLLIDING_JSON[8:]], ['[REDACTED]']),
])
def test_normalized_json_member_collision_fails_closed(value, expected):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    result = boundary.export({'note': value})
    assert result == {'note': expected}


@pytest.mark.parametrize('control', ['\x00', '\t', '\u200b'])
def test_concealed_credential_keys_use_normalized_classification(control):
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({'p' + control + 'assword': 'owned-primary',
                            'input_' + control + 'tokens': 17, 'ordinary' + control: 'safe'}) == {
                                'input_' + control + 'tokens': 17, 'ordinary' + control: 'safe'}


@pytest.mark.parametrize('control', ['\t', '\n', '\r', '\x85', '\xa0', '\u2028', '\t\u200b'])
@pytest.mark.parametrize('layers', [0, 1, 2])
def test_registered_scheme_cannot_hide_secondary_in_decoded_carriers(control, layers):
    carrier = 'Be' + control + 'arer owned-secondary'
    for _ in range(layers):
        carrier = json.dumps(carrier)
    result = CredentialScrubber(secret_values=('Bearer',)).capture(carrier.encode())
    assert 'owned-secondary' not in result
    assert '[REDACTED]' in result


def test_ordinary_ascii_space_inside_word_remains_prose():
    text = 'Be arer ordinary public text'
    assert EmissionBoundary(ContentPolicy(True), secrets=('Bearer',)).export({'note': text}) == {'note': text}
    assert CredentialScrubber(secret_values=('Bearer',))(text) == text


def test_registration_generator_keeps_printable_alias():
    from api_agent.telemetry.redaction import registered_values
    assert registered_values(value for value in ['owned-\tcredential']) == {'owned-\tcredential', 'owned-credential'}


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('secret, carrier', [
    ('owned-\tcredential-remaining', r'owned-\u0009credential-remaining END'),
    ('owned-\u200bcredential-remaining', r'owned-\u200Bcredential-remaining END'),
    ('owned prefix password=owned-primary suffix-owned', r'o\u0077ned prefix password=owned-primary suffix-owned END'),
    ('https://owned-user:owned-primary@public.invalid/suffix-owned', r'https://owned-user:owned-primary@public.invalid/suffix-\u006fwned END'),
])
def test_bootstrap_terminal_protects_original_encoded_credential(
        terminal_failure_launch, tmp_path, monkeypatch, capsys, stream, secret, carrier):
    from api_agent.runtime.bootstrap import main
    from test_terminal_failure_diagnostics import ARGS, ROOT
    helper = tmp_path / 'owned_output.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), os.environ["OWNED_TEXT"].encode())\nsys.exit(7)\n')
    finalized = []
    original_finalize = prerequisites.finalize
    def finalize(process):
        finalized.append(process)
        original_finalize(process)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    def fail_integration(*args, **kwargs):
        prerequisites.run_command((sys.executable, str(helper), str(stream)),
            environment={'API_KEY': secret, 'OWNED_TEXT': carrier}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)
    monkeypatch.setattr('api_agent.testing.launched.run_launched_integration', fail_integration)

    assert main(ARGS, base_root=ROOT) == 1

    terminal = capsys.readouterr().err
    assert 'owned-' not in terminal and 'suffix-owned' not in terminal
    assert '[REDACTED] END' in terminal
    assert 'Exit code: 7' in terminal
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


@pytest.mark.parametrize('carrier', [
    'ordinary {"note":"not-the-secret',
    'ordinary {"omega":8',
    'ordinary ["other-value"',
    'ordinary "public prose',
    'ordinary {"note":"not-the-secret"} END',
])
def test_unrelated_partial_json_remains_ordinary(carrier):
    scrubber = CredentialScrubber({'API_KEY': '{"omega":9}'})
    assert scrubber.capture(carrier.encode()) == carrier


@pytest.mark.parametrize('secret, carrier', [
    ('{"omega":9}', 'ordinary {"omega": 9'),
    ('{"count":3,"omega":"owned-credential"}', 'ordinary {"omega": "owned-cred'),
    ('["owned-credential"]', 'ordinary [ "owned-cred'),
])
def test_relevant_semantic_json_prefix_is_protected(secret, carrier):
    assert CredentialScrubber({'API_KEY': secret}).capture(carrier.encode()) == 'ordinary [REDACTED]'


@pytest.mark.parametrize('secret,value', [('4\t2', 42), ('T\u200brue', True), ('4\x002.5', 42.5)])
def test_primitive_exports_use_normalized_registered_values(secret, value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'note': value}) == {}


@pytest.mark.parametrize('secrets', [(), ('unrelated-owned-value',)])
@pytest.mark.parametrize('carrier', [
    b'"Be\\u0061rer owned-secondary"',
    b'"Be\\t\\u200Barer owned-secondary"',
    b'"Bearer\\u0020owned-secondary"',
    b'"Bearer\\u0009owned-secondary"',
    b'Bearer owned-secondary',
    b'Be\x00arer owned-secondary',
])
def test_recognizable_encoded_scheme_does_not_depend_on_registration(secrets, carrier):
    result = CredentialScrubber(secret_values=secrets).capture(carrier)
    assert 'owned-secondary' not in result
    assert '[REDACTED]' in result


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('control, escape', [('\t', r'\u0009'), ('\u200b', r'\u200B')])
def test_alternate_genuine_control_capture_at_response_bound(tmp_path, monkeypatch, stream, control, escape):
    secret = 'owned-' + control + 'credential-' + 'q' * 3000 + '-remaining'
    carrier = ('owned-' + escape + 'credential-' + 'q' * 3000 + '-remaining').encode()
    helper = tmp_path / 'cutoff_writer.py'
    helper.write_text('import os, sys\nfd = int(sys.argv[1])\ndata = b"x" * (8 * 1024 * 1024 - 2500) + os.environ["OWNED_TEXT"].encode()\nwhile data:\n n = os.write(fd, data)\n data = data[n:]\n')
    response_bound = 8 * 1024 * 1024
    tail_start = response_bound - 2500
    original_read = prerequisites.os.read
    offsets, reads = {}, {}
    def read_credential_tail(fd, size):
        offset = offsets.get(fd, 0)
        limit = min(size, tail_start - offset) if offset < tail_start else min(size, 127)
        chunk = original_read(fd, limit)
        if chunk:
            reads.setdefault(fd, []).append((offset, len(chunk)))
            offsets[fd] = offset + len(chunk)
        return chunk
    monkeypatch.setattr(prerequisites.os, 'read', read_credential_tail)
    finalized = []
    original_finalize = prerequisites.finalize
    def finalize(process):
        finalized.append(process)
        original_finalize(process)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)

    with pytest.raises(prerequisites.PrerequisiteError, match='safe bound') as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream)),
            environment={'API_KEY': secret, 'OWNED_TEXT': carrier.decode()}, directory=tmp_path,
            deadline=time.monotonic() + 20, cancelled=lambda: False)

    output_reads, = (rows for fd, rows in reads.items() if offsets[fd] > response_bound)
    prefix_reads = [(offset, size) for offset, size in output_reads if offset < tail_start]
    tail_reads = [(offset, size) for offset, size in output_reads if offset >= tail_start]
    assert any(size > 127 for _, size in prefix_reads)
    assert all(offset + size <= tail_start for offset, size in prefix_reads)
    assert tail_reads[0][0] == tail_start
    assert len(tail_reads) > 1 and all(size <= 127 for _, size in tail_reads)
    assert response_bound < sum(size for _, size in output_reads) <= response_bound + 127
    assert 'owned-' not in caught.value.local_details
    assert 'q' * 100 not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


@pytest.mark.parametrize('control', ['\x00', '\t', '\u200b'])
@pytest.mark.parametrize('layers', [1, 2])
def test_encoded_credential_key_is_classified_before_source_normalization(control, layers):
    carrier = json.dumps({'p' + control + 'assword': 'owned-secondary', 'ordinary': 'safe'})
    for _ in range(layers - 1):
        carrier = json.dumps(carrier)
    result = CredentialScrubber().capture(carrier.encode())
    assert 'owned-secondary' not in result
    assert '[REDACTED]' in result


@pytest.mark.parametrize('control', ['\x00', '\t', '\u200b'])
def test_encoded_control_accounting_key_keeps_ordinary_content(control):
    carrier = json.dumps({'input_' + control + 'tokens': 17, 'ordinary': 'safe'})
    assert CredentialScrubber().capture(carrier.encode()) == carrier


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('control, escape', [('\t', r'\u0009'), ('\u200b', r'\u200B')])
@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('termination', ['cancel', 'deadline', 'cleanup'])
def test_interrupted_genuine_control_capture_tracks_original_value(
        tmp_path, monkeypatch, stream, control, escape, partial, termination):
    secret = 'owned-' + control + 'credential-remaining'
    suffix = 'credential-rem' if partial else 'credential-remaining END'
    carrier = 'owned-' + escape + suffix
    helper = tmp_path / 'interrupted_writer.py'
    helper.write_text('import os, sys, time\nos.write(int(sys.argv[1]), os.environ["OWNED_TEXT"].encode())\ntime.sleep(0 if sys.argv[2] == "cleanup" else 30)\nsys.exit(7)\n')
    observed, finalized = [], []
    original_read, original_finalize = prerequisites.os.read, prerequisites.finalize
    def read(fd, size):
        value = original_read(fd, size)
        if value:
            observed.append(value)
        return value
    def finalize(process):
        finalized.append(process)
        original_finalize(process)
        if termination == 'cleanup':
            raise RuntimeError('owned post-disposal failure')
    monkeypatch.setattr(prerequisites.os, 'read', read)
    monkeypatch.setattr(prerequisites, 'finalize', finalize)
    now = lambda: 2 if observed and termination == 'deadline' else 0

    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream), termination),
            environment={'API_KEY': secret, 'OWNED_TEXT': carrier}, directory=tmp_path,
            deadline=1, cancelled=lambda: bool(observed) and termination == 'cancel', now=now)

    assert 'owned-' not in caught.value.local_details
    assert 'credential-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details
    assert len(finalized) == 1
    assert finalized[0]._api_agent_disposal_complete
    assert finalized[0].stdout.closed and finalized[0].stderr.closed


@pytest.mark.parametrize('layers', [0, 1, 2])
@pytest.mark.parametrize('secret,carrier', [
    (json.dumps({'owned-\tkey': '€'}, separators=(',', ':')), r'{"\u006fwned-\t\u0000ke'),
    (json.dumps({'count': 3, 'owned-\tkey': '€'}), r'{ "owned-\u0009ke'),
    (json.dumps(['owned-\tvalue']), r'[ "owned-\u0009val'),
    (json.dumps('owned-\tvalue'), r'"owned-\u0009val'),
    (json.dumps({'owned-key': 'owned-\tvalue'}), r'{ "owned-key" : "owned-\u0009val'),
    ('4\t2.75', r'\u0034\u0032.'),
])
def test_structural_key_value_and_scalar_cuts_follow_registered_contract(layers, secret, carrier):
    for _ in range(layers):
        carrier = json.dumps(carrier).rstrip('"')
    result = CredentialScrubber({'API_KEY': secret}).capture(('🧩 context / ' + carrier).encode())
    assert result.startswith('🧩 context / ')
    assert 'owned-' not in result
    assert '[REDACTED]' in result


@pytest.mark.parametrize('operation', ['registration', 'prefix', 'credential_key'])
def test_recursive_serialized_guards_reject_excessive_nesting(operation):
    from api_agent.telemetry import redaction
    value = 'ordinary'
    for _ in range(22):
        value = json.dumps(value)
    operations = {
        'registration': lambda: redaction._contains_json_registration(value, {'"owned"'}),
        'prefix': lambda: redaction._contains_json_prefix(value, {'"owned"'}),
        'credential_key': lambda: redaction._contains_credential_key(value, lambda key: key == 'password', concealed_only=False),
    }
    with pytest.raises(redaction.AmbiguousJSON, match='nesting'):
        operations[operation]()


def test_structural_depth_bound_fails_closed_without_discarding_adjacent_execution():
    carrier = ('[' * 22 + '"ordinary"' + ']' * 22).encode()
    assert CredentialScrubber().capture(carrier) == '[REDACTED]'


@pytest.mark.parametrize('operation', ['registration', 'complete', 'prefix', 'credential_key'])
def test_duplicate_decoded_members_fail_closed_in_each_semantic_parser(operation):
    from api_agent.telemetry import redaction
    duplicate = '{"omega":9,"omega":3}'
    operations = {
        'registration': lambda: redaction._json_registrations((duplicate,)),
        'complete': lambda: redaction._completed_json(duplicate),
        'prefix': lambda: redaction._contains_json_registration(duplicate, {'"owned"'}),
        'credential_key': lambda: redaction._contains_credential_key(duplicate, lambda key: False, concealed_only=False),
    }
    with pytest.raises(redaction.AmbiguousJSON, match='Duplicate'):
        operations[operation]()


@pytest.mark.parametrize('carrier,expected', [
    ('{"omega":[9]}', {'omega': [9]}),
    ('{"omega":[9}', None),
    ('}', None),
])
def test_incomplete_container_validation_preserves_balanced_structure(carrier, expected):
    from api_agent.telemetry import redaction
    assert redaction._completed_json(carrier) == expected


def test_duplicate_semantic_diagnostic_members_fail_closed_without_registration():
    assert CredentialScrubber().capture(b'{"omega":9,"omega":3}') == '[REDACTED]'


def test_invalid_cut_member_escape_is_ordinary_and_not_a_registered_prefix():
    from api_agent.telemetry import redaction
    assert not redaction._partial_member_prefix('{"\\q', {'{"omega":9}'})


def test_primitive_only_registration_does_not_allocate_semantic_carrier_ranges():
    from api_agent.telemetry import redaction
    assert tuple(redaction._semantic_byte_ranges(b'ordinary "text"', set(), False, None, False)) == ()


def test_nested_native_json_depth_is_bounded_before_canonical_serialization():
    from api_agent.telemetry import redaction
    value = 9
    for _ in range(22):
        value = [value]
    with pytest.raises(redaction.AmbiguousJSON, match='nesting'):
        redaction.normalized_json(value)


def test_lone_surrogate_capture_fails_closed_without_encoding_exception():
    assert CredentialScrubber().capture(b'ordinary') == 'ordinary'
    from api_agent.telemetry import redaction
    assert redaction.registered_text_ranges('owned-\ud800', ('owned',)) == ((0, 7),)


def test_registered_interval_union_retains_transitive_overlap_after_expansion():
    assert _RegisteredRanges._merge([(2, 5), (4, 8), (7, 11), (15, 17)]) == [(2, 11), (15, 17)]


@pytest.mark.parametrize('layers', [0, 1, 2])
@pytest.mark.parametrize('escape', [r'\ud83d', r'\ud83d\u', r'\ud83d\ude', r'\ude00'])
def test_lone_serialized_surrogate_cannot_hide_a_registered_cut_prefix(layers, escape):
    secret = 'owned-€-😀-credential-remaining'
    carrier = r'\u006fwned-\u20ac-' + escape
    for _ in range(layers):
        carrier = json.dumps(carrier)
    result = CredentialScrubber({'API_KEY': secret}).capture(carrier.encode())
    assert 'owned-' not in result
    assert r'\u006f' not in result
    assert '[REDACTED]' in result


def test_valid_surrogate_pair_keeps_its_complete_credential_and_original_offsets():
    secret = 'owned-😀-credential'
    assert CredentialScrubber({'API_KEY': secret}).capture(b'context "owned-\\ud83d\\ude00-credential" END') == 'context "[REDACTED]" END'


@pytest.mark.parametrize('secret,value', [
    ('true', True), ('false', False), ('null', None),
    ('t\x00rue', True), ('f\u200balse', False), ('n\tull', None),
    ('True', True), ('False', False), ('None', None),
])
def test_native_scalar_exports_use_both_registered_spelling_contracts(secret, value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'note': value}) == {}
    assert boundary._decoded_value_credential(value, 0)


@pytest.mark.parametrize('value', [True, False, None, 42, 42.5])
def test_unregistered_native_scalar_remains_exact(value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('ordinary-owned-value',))
    assert boundary.export({'note': value}) == {'note': value}
    assert not boundary._decoded_value_credential(value, 0)


@pytest.mark.parametrize('secret,value,expected', [
    ('{"omega":9}', {'omega': 9}, {}),
    ('["owned-json"]', ['owned-json'], {}),
    ('"owned-json"', 'owned-json', {'note': '[REDACTED]'}),
    ('{"ome\\tga":9}', {'ome\x00ga': 9}, {}),
    ('["owned-\\tjson"]', ['owned-\u200bjson'], {}),
    ('"owned-\\tjson"', 'owned-\x00json', {'note': '[REDACTED]'}),
    ('{"password":"owned-primary","omega":9}', {'password': 'owned-primary', 'omega': 9}, {}),
])
def test_original_native_json_nodes_follow_registered_representation_contract(secret, value, expected):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'note': value}) == expected


@pytest.mark.parametrize('value', [{'omega': 8}, ['ordinary-json'], 'ordinary-json'])
def test_unrelated_native_json_nodes_remain_exact(value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}', '["owned-json"]', '"owned-json"'))
    assert boundary.export({'note': value}) == {'note': value}


def test_native_normalized_key_collision_cannot_discard_a_credential_member():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export({'note': {'omega': 9, 'ome\x00ga': 'ordinary'}}) == {}


@pytest.mark.parametrize('secret,value,expected', [
    ('1e3', 1000.0, {}), ('1\x00e3', 1000.0, {}),
    ('"owned\\u002djson"', 'owned-json', {'note': '[REDACTED]'}),
])
def test_native_json_equivalence_uses_decoded_registration_spellings(secret, value, expected):
    assert EmissionBoundary(ContentPolicy(True), secrets=(secret,)).export({'note': value}) == expected


def test_registering_new_secret_invalidates_native_json_comparison_cache():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export({'note': {'count': 3}}) == {'note': {'count': 3}}
    boundary.register_secret('{"count":3}')
    assert boundary.export({'note': {'count': 3}}) == {}


def test_native_comparison_preserves_safe_siblings_of_unsupported_values():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export({'note': {'ordinary': 'safe', 'unsupported': object(), 17: 'ordinary'}}) == {'note': {'ordinary': 'safe'}}


def test_shared_native_json_comparison_accepts_uncached_callers():
    from api_agent.telemetry.redaction import registered_json_value
    assert registered_json_value({'omega': 9}, ('{"omega":9}',))


def test_native_registered_node_removal_preserves_ordinary_enclosing_members():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export({'note': {'omega': 9}, 'ordinary': 'safe'}) == {'ordinary': 'safe'}


@pytest.mark.parametrize('secret,value', [
    ('{"ordinary":"retained"}', {17: 'discarded', 'ordinary': 'retained'}),
    ('{"ordinary":"retained"}', {'unsupported': object(), 'ordinary': 'retained'}),
    ('{"ordinary":"retained"}', {'nonfinite': float('inf'), 'ordinary': 'retained'}),
    ('[17]', [object(), 17]), ('[17]', [float('inf'), 17]), ('[17]', (17,)),
])
def test_native_filtering_cannot_form_a_registered_json_node(secret, value):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    assert boundary.export({'note': value, 'ordinary': 'safe'}) == {'ordinary': 'safe'}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('secret,payload', [
    ('{"duration_ms":7}', {'duration_ms': 7}),
    ('{"duration_ms":7}', {'duration_ms': 7, 17: 'ignored'}),
    ('{"duration_ms":7}', {'duration_ms': 7, 'password': 'discarded'}),
])
def test_registered_export_root_is_protected_before_and_after_filtering(enabled, secret, payload):
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=(secret,))
    assert boundary.export(payload) == {}


@pytest.mark.parametrize('payload', [{'omega': 9}, {'omega': 9, 17: 'ignored'}])
def test_content_enabled_registered_export_root_uses_generic_object_semantics(payload):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export(payload) == {}


@pytest.mark.parametrize('enabled', [False, True])
def test_unregistered_export_root_retains_ordinary_metadata(enabled):
    boundary = EmissionBoundary(ContentPolicy(enabled), secrets=('{"duration_ms":7}',))
    assert boundary.export({'duration_ms': 8}) == {'duration_ms': 8}


def test_cyclic_native_candidate_preserves_unrelated_retained_members():
    value = {'ordinary': 'retained'}
    value['cycle'] = value
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    assert boundary.export({'note': value}) == {'note': {'ordinary': 'retained'}}


def test_cyclic_native_list_preserves_unrelated_retained_elements():
    value = ['retained']
    value.append(value)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    assert boundary.export({'note': value}) == {'note': ['retained']}


def test_deep_native_candidate_uses_existing_recursive_omission_policy():
    value = {'ordinary': 'retained'}
    deep = 'ordinary'
    for _ in range(24):
        deep = {'nested': deep}
    value['deep'] = deep
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    ordinary = EmissionBoundary(ContentPolicy(True)).export({'note': value})
    assert boundary.export({'note': value}) == ordinary


def test_original_serialized_semantic_match_retains_established_marker():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    text = json.dumps({'payload': {'omega': 9}})
    assert boundary.export({'note': text}) == {'note': '[REDACTED]'}


def test_deep_serialized_candidate_uses_existing_recursive_omission_policy():
    value = {'ordinary': 'retained'}
    deep = 'ordinary'
    for _ in range(24):
        deep = {'nested': deep}
    value['deep'] = deep
    text = json.dumps(value)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('true',))
    ordinary = EmissionBoundary(ContentPolicy(True)).export({'note': text})
    assert boundary.export({'note': text}) == ordinary


def test_serialized_canonicalization_cannot_form_a_registered_member_fragment():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('"omega": 9',))
    assert boundary.export({'note': '{"omega":9}'}) == {'note': '[REDACTED]'}


def test_registration_during_lazy_parse_cannot_publish_a_current_stale_cache(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from api_agent.telemetry import privacy
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('ordinary-owned-value',))
    parsed = Barrier(2)
    publish = Barrier(2)
    original = privacy._json_registrations
    first = True

    def controlled_parse(secrets):
        nonlocal first
        registrations = original(secrets)
        if first:
            first = False
            parsed.wait(timeout=5)
            publish.wait(timeout=5)
        return registrations

    monkeypatch.setattr(privacy, '_json_registrations', controlled_parse)
    with ThreadPoolExecutor(max_workers=1) as executor:
        warm = executor.submit(boundary.export, {'ordinary': 1})
        parsed.wait(timeout=5)
        boundary.register_secret('{"omega":9}')
        publish.wait(timeout=5)
        assert warm.result(timeout=5) == {'ordinary': 1}
    assert boundary.export({'omega': 9}) == {}
    assert boundary.export({'note': {'omega': 9}, 'ordinary': 'safe'}) == {'ordinary': 'safe'}


def test_repeated_registration_keeps_serial_native_cache_correct():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"omega":9}',))
    assert boundary.export({'ordinary': 1}) == {'ordinary': 1}
    boundary.register_secret('{"omega":9}')
    assert boundary.export({'omega': 9}) == {}
    assert boundary.export({'duration_ms': 8}) == {'duration_ms': 8}
