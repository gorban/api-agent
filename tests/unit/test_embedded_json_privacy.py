"""Complete credential-bearing fragments remain sensitive in mixed text."""
import json

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


@pytest.mark.parametrize('key', ['access_token', r'access_\u0074oken'])
@pytest.mark.parametrize('surrounding', [('', '.'), ('log ', ' suffix'), ('', ' {}')])
@pytest.mark.parametrize('payload_for', [lambda text: {'text': text},
                                       lambda text: {'parts': [text[:5], text[5:]]}], ids=['contiguous', 'split'])
def test_complete_credential_fragment_survives_whole_text_parse_failure(key, surrounding, payload_for):
    text = surrounding[0] + '{"' + key + '":"owned-secondary-value"}' + surrounding[1]
    actual = EmissionBoundary(ContentPolicy(True)).export(payload_for(text))
    assert 'owned-secondary-value' not in json.dumps(actual)


@pytest.mark.parametrize('layers', [0, 1, 2])
def test_recursive_complete_fragments_preserve_registered_container_checks(layers):
    text = '{"note":{"ordinary":17}}'
    for _ in range(layers):
        text = json.dumps(text)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('{"ordinary":17}',))
    actual = boundary.export({'parts': [text[:5], text[5:] + '.']})
    assert actual == {'parts': ['[REDACTED]']}


@pytest.mark.parametrize('text', ['{"note":"or\\u0064inary"}.', 'log {"ordinary":17} tail',
                                 '{broken} "ordinary".', '{"ordinary":"unfinished'])
def test_ordinary_malformed_text_remains_exact(text):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('owned-primary',), preserve_json_text=True)
    assert boundary.export({'text': text}) == {'text': text}
    assert boundary.content_export({'parts': [text[:5], text[5:]]}) == {'parts': [text[:5], text[5:]]}


def test_malformed_encoded_key_retains_credential_role_detection():
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({'"password".': 'owned-secondary-value', 'ordinary': 'safe'}) == {'ordinary': 'safe'}


def test_malformed_ordinary_encoded_key_preserves_its_spelling():
    assert EmissionBoundary(ContentPolicy(True)).export({'"ordinary".': 'safe'}) == {'"ordinary".': 'safe'}


def test_overdeep_fragment_scan_fails_closed():
    text = 'log ' + '[' * 21 + '0' + ']' * 21 + '.'
    assert EmissionBoundary(ContentPolicy(True)).export({'text': text}) == {'text': '[REDACTED]'}


def test_ambiguous_complete_fragment_in_log_text_fails_closed():
    text = 'log {"ordinary":1,"ordinary":2} end'
    assert EmissionBoundary(ContentPolicy(True)).export({'text': text}) == {'text': '[REDACTED]'}
