"""Registered credentials remain private across encoding and capture seams."""
import json
import sys
import time

import pytest

from api_agent.runtime import prerequisites
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


@pytest.mark.parametrize('control', ['\x00', '\t', '\n', '\u200b'])
@pytest.mark.parametrize('container', [lambda text: text, lambda text: {'note': text}, lambda text: [text]],
                         ids=['text', 'object', 'array'])
def test_encoded_registered_insertions_are_masked(control, container):
    secret = 'synthetic-owned-value'
    concealed = secret[:8] + control + secret[8:]
    value = container(concealed)
    encoded = json.dumps(value)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))

    result = boundary.content_export({'parts': [encoded[:7], encoded[7:]]})

    assert result == {'parts': ['[REDACTED]']}


def test_printable_detection_retains_original_offsets_and_controls():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('owned-value',))
    result = boundary.export({'note': '\tordinary\x00 owned-\tvalue \u200bending'})
    assert result == {'note': '\tordinary\x00 [REDACTED] \u200bending'}


@pytest.mark.parametrize('captured,secrets,expected', [
    (b'ordinary end', ('credential',), b'ordinary end'),
    (b'ordinary c', ('credential',), b'ordinary [REDACTED]'),
    (b'ordinary x', ('x',), b'ordinary x'),
    (b'context owned-\xe2\x80\x8beuro-\xe2\x82', ('owned-euro-€-remaining',), b'context [REDACTED]'),
    (b'context \"owned-\\u200beuro-\\u20ac-re', ('owned-euro-€-remaining',), b'context \"[REDACTED]'),
    (b'context \"owned-\\teuro-\\u20', ('owned-euro-€-remaining',), b'context \"[REDACTED]'),
    (b'context \"ab\\taba\" end', ('abab',), b'context \"[REDACTED]a\" end'),
    (b'left abc middle abc right', ('abc', 'right-end'), b'left abc middle abc [REDACTED]'),
    (b'ordinary', (), b'ordinary'),
    (b'context owned-', ('owned-\udcff',), b'context [REDACTED]'),
    (b'ordinary', ('owned-\ud800',), b'[REDACTED]'),
    (b'ordinary\t', ('credential',), b'ordinary\t'),
    (b'ordinary \\z', ('credential',), b'ordinary \\z'),
    (b'context owned-\\ud83d\\ude00-e', ('owned-😀-end',), b'context [REDACTED]'),
    (b'context \\ud800 owned-', ('owned-secret',), b'context \\ud800 [REDACTED]'),
    (b'context \\u0041\\u0042', ('A',), b'context [REDACTED]\\u0042'),
    (b'', ('credential',), b''),
    (b'context abcdefghi', ('abcdef', 'defghi', 'ghi-end'), b'context [REDACTED]'),
    (b'context ababa', ('abab',), b'context [REDACTED]'),
    (b'context ab\taba', ('abab',), b'context [REDACTED]'),
    (b'context owned-\xe2\x80\x8beuro-\xe2\x82\xac-re', ('owned-euro-€-remaining',), b'context [REDACTED]'),
    (b'context abcdefg', ('cdefgh', 'abcdefgh'), b'context [REDACTED]'),
    (b'context ' + 'owned-€'.encode()[:-1], ('owned-€-suffix',), b'context [REDACTED]'),
    (b'q' * 10000 + b'z', ('q' * 10000 + 'y',), b'q' * 10000 + b'z'),
    (b'context ' + b'q' * 9999, ('q' * 10000 + 'y',), b'context [REDACTED]'),
])
def test_incomplete_capture_prefix_union(captured, secrets, expected):
    assert prerequisites._mask_incomplete_secret(captured, secrets, complete=True) == expected


@pytest.mark.parametrize('layers', [1, 2, 3])
def test_complete_encoded_registered_value_preserves_independent_scheme(layers):
    secret = 'owned-euro-€-remaining'
    encoded = 'owned-\u200beuro-€-remaining'
    for _ in range(layers):
        encoded = json.dumps(encoded)
    captured = ('https://user:password@public.invalid before ' + encoded + ' after https').encode()

    result = prerequisites._mask_incomplete_secret(captured, (secret, 'https'), complete=True)

    assert b'owned-' not in result
    assert b'https://user:password@public.invalid before' in result
    assert result.endswith(b' after https')
    assert b'[REDACTED]' in result


@pytest.mark.parametrize('layers', [1, 2, 3])
@pytest.mark.parametrize('stream', [1, 2])
def test_complete_serialized_diagnostic_retains_no_registered_credential(tmp_path, layers, stream):
    secret = 'owned-euro-€-remaining'
    encoded = 'owned-\u200beuro-€-remaining'
    for _ in range(layers):
        encoded = json.dumps(encoded)
    helper = tmp_path / 'complete_carrier.py'
    helper.write_text('import os, sys\nos.write(int(sys.argv[1]), os.environ["OUTPUT"].encode())\nsys.exit(1)\n')

    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream)),
            environment={'API_KEY': secret, 'OUTPUT': encoded}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)

    assert 'owned-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details


def test_complete_encoded_guard_runs_after_secondary_uri_scrubbing(tmp_path):
    registered = 'https://synthetic-user'
    output = r'https://synthetic-\u0075ser:secondary-secret@example.test/path'
    helper = tmp_path / 'encoded_uri.py'
    helper.write_text('import os, sys\nprint(os.environ["OUTPUT"], file=sys.stderr)\nsys.exit(1)\n')

    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper)),
            environment={'API_KEY': registered, 'OUTPUT': output}, directory=tmp_path,
            deadline=time.monotonic() + 5, cancelled=lambda: False)

    assert 'secondary-secret' not in caught.value.local_details
    assert 'synthetic-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details


@pytest.mark.parametrize('layers', [1, 2, 3])
def test_nested_incomplete_serialized_prefix_is_masked(layers):
    secret = 'owned-euro-€-remaining'
    encoded = 'owned-\u200beuro-€-re'
    for _ in range(layers):
        encoded = json.dumps(encoded)
    captured = ('ordinary ' + encoded.rstrip('"')).encode()

    result = prerequisites._mask_incomplete_secret(captured, (secret,))

    assert b'owned-' not in result
    assert b'[REDACTED]' in result


@pytest.mark.parametrize('layers', [20, 21])
def test_escape_detection_depth_limit_is_fail_closed(layers):
    secret = 'owned-euro-€-remaining'
    encoded = 'owned-\\u200beuro-\\u20ac-re'
    for _ in range(layers - 1):
        encoded = encoded.replace('\\', '\\u005c')

    result = prerequisites._mask_incomplete_secret(encoded.encode(), (secret,))

    assert b'owned-' not in result
    assert b'[REDACTED]' in result


@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('read_size', [127, 65536])
def test_response_bound_masks_registered_prefix(tmp_path, monkeypatch, stream, read_size):
    secret = 'owned-prefix-' + 'q' * 3000 + '-owned-end'
    helper = tmp_path / 'writer.py'
    helper.write_text('import os, sys\nfd = int(sys.argv[1])\ndata = b"x" * (8 * 1024 * 1024 - 2500) + os.environ["API_KEY"].encode()\nwhile data:\n n = os.write(fd, data)\n data = data[n:]\n')
    original = prerequisites.os.read
    monkeypatch.setattr(prerequisites.os, 'read', lambda fd, size: original(fd, min(size, read_size)))

    with pytest.raises(prerequisites.PrerequisiteError, match='safe bound') as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream)),
            environment={'API_KEY': secret}, directory=tmp_path,
            deadline=time.monotonic() + 20, cancelled=lambda: False)

    assert 'owned-prefix-' not in caught.value.local_details
    assert 'q' * 100 not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details


@pytest.mark.parametrize('interruption', ['cancel', 'deadline'])
@pytest.mark.parametrize('stream', [1, 2])
@pytest.mark.parametrize('insertion', ['', '\t', '\u200b'])
def test_interrupted_capture_masks_partial_utf8_secret(tmp_path, monkeypatch, interruption, stream, insertion):
    secret = 'owned-€-remaining'
    helper = tmp_path / 'partial.py'
    helper.write_text('import os, sys, time\nos.write(int(sys.argv[1]), (os.environ["API_KEY"][:6] + os.environ["INSERTION"] + os.environ["API_KEY"][6:]).encode()[:8 + len(os.environ["INSERTION"].encode())])\ntime.sleep(30)\n')
    observed = []
    original = prerequisites.os.read
    def read(fd, size):
        value = original(fd, size)
        if value:
            observed.append(value)
        return value
    monkeypatch.setattr(prerequisites.os, 'read', read)
    now = lambda: 2 if observed and interruption == 'deadline' else 0

    with pytest.raises(prerequisites.PrerequisiteError) as caught:
        prerequisites.run_command((sys.executable, str(helper), str(stream)),
            environment={'API_KEY': secret, 'INSERTION': insertion}, directory=tmp_path, deadline=1,
            cancelled=lambda: bool(observed) and interruption == 'cancel', now=now)

    assert 'owned-' not in caught.value.local_details
    assert '[REDACTED]' in caught.value.local_details
