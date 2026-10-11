"""Inert test declarations retain bounded reads without obsolete selection cases."""
from pathlib import Path
import os

import pytest

from api_agent.testing.collection import CollectionError
from api_agent.testing.provider_tests import load_test_declaration


def provider(tmp_path, declaration='version=1\n[tests]\nintegration=[]\n'):
    root = tmp_path / 'provider'
    (root / 'cfg').mkdir(parents=True)
    (root / 'factory.py').write_text('raise AssertionError("factory must remain inert")\n')
    (root / 'cfg/tests.toml').write_text(declaration)
    return root


def test_inert_declaration_and_absent_manifest(tmp_path):
    root = provider(tmp_path)
    assert load_test_declaration(root) == {'integration': []}
    (root/'cfg/tests.toml').unlink()
    assert load_test_declaration(root) is None


@pytest.mark.parametrize('declaration', [
    'version=1\n[tests]\nintegration=[]\nrequest_selections=[]',
    'version=1\n[tests]\nintegration=[]\nextra=true',
])
def test_obsolete_or_unknown_test_metadata_is_rejected(tmp_path, declaration):
    with pytest.raises(CollectionError, match='Invalid provider test declaration'):
        load_test_declaration(provider(tmp_path, declaration))


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'oversized', 'invalid-utf8', 'invalid-toml', 'symlink-escape'])
def test_nonregular_oversized_and_invalid_declarations_fail_promptly(tmp_path, kind):
    root = provider(tmp_path)
    file = root/'cfg/tests.toml'
    file.unlink()
    if kind == 'fifo':
        os.mkfifo(file)
    elif kind == 'directory':
        file.mkdir()
    elif kind == 'oversized':
        file.write_text('#' + 'a'*262144)
    elif kind == 'invalid-utf8':
        file.write_bytes(b'\xff')
    elif kind == 'invalid-toml':
        file.write_text('not=[valid')
    else:
        outside = tmp_path/'outside.toml'
        outside.write_text('version=1\n[tests]\nintegration=[]')
        file.symlink_to(outside)
    with pytest.raises(CollectionError):
        load_test_declaration(root)


def test_manifest_read_error_is_actionable(tmp_path, monkeypatch):
    root = provider(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('cannot read')
    monkeypatch.setattr('api_agent.testing.provider_tests.os.open', fail)
    with pytest.raises(CollectionError, match='Cannot read provider test declaration'):
        load_test_declaration(root)


def test_removed_opt_in_is_not_declared_and_is_rejected_as_unknown():
    from api_agent.config.declarations import load_declarations
    from api_agent.config.options import parse_options
    root = Path(__file__).parents[2]
    schema = load_declarations(root/'cfg/config.toml')
    assert 'INTEGRATION_LLM_SELECTION' not in schema.fields
    options = parse_options(schema, ['--integration-llm-selection=gpt-example'])
    assert options.errors == ('Unknown option --integration-llm-selection',)
    assert not options.values
