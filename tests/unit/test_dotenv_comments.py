"""Quoted dotenv comments remain literal data without shell expansion."""
import pytest

from api_agent.config.dotenv import decode_value, read_dotenv


@pytest.mark.parametrize(('text', 'expected'), [
    ('"value" # comment', 'value'),
    ("'value'\t# comment with ' and \"", 'value'),
    ('"embedded#literal" # comment', 'embedded#literal'),
    ('"$(command) ${NAME}" # comment', '$(command) ${NAME}'),
    ('"a\\\"b" # comment', 'a\\"b'),
    ('"a\\\\" # comment', 'a\\\\'),
    ('"" # comment', ''),
    ('"value"', 'value'),
    ('plain#literal', 'plain#literal'),
    ('"a\\nb" # api-agent-escaped', 'a\nb'),
])
def test_quoted_comment_decodes_only_literal_content(text, expected):
    assert decode_value(text) == expected


@pytest.mark.parametrize('text', [
    '"unclosed', '"', '"escaped\\"',
    '"value"#comment', '"value" trailing', '"value" "suffix"',
    '"value" \tother # comment',
    '7 # api-agent-escaped', 'bad # api-agent-escaped',
])
def test_malformed_quote_or_suffix_is_rejected(text):
    with pytest.raises(ValueError):
        decode_value(text)


def test_reader_preserves_quoted_hash_and_ignores_comment_quotes(tmp_path):
    source = tmp_path / 'synthetic.env'
    source.write_text('# heading\n\nexport VALUE="literal#value" # "ignored quote\nOTHER=plain\n')
    assert read_dotenv(source) == {'VALUE': 'literal#value', 'OTHER': 'plain'}
    source.write_text('VALUE="unclosed # comment\n')
    with pytest.raises(ValueError, match='synthetic.env:1: unclosed quoted value'):
        read_dotenv(source)
    source.write_text('invalid line\n')
    with pytest.raises(ValueError, match='expected KEY=value'):
        read_dotenv(source)
    assert read_dotenv(tmp_path / 'missing.env') == {}
