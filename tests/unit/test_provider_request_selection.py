"""Provider-authored inference cases are inert, bounded and independent of catalogs."""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from api_agent.providers.contracts import SetupPrerequisiteError
from api_agent.testing.collection import CollectionError, provider_integration_suites
from api_agent.testing.provider_tests import load_test_declaration, request_selection


def provider(tmp_path, declaration='version=1\n[tests]\nintegration=[]\n'):
    root = tmp_path / 'provider'
    (root / 'cfg').mkdir(parents=True)
    (root / 'factory.py').write_text('raise AssertionError("factory must remain inert")\n')
    (root / 'cfg/tests.toml').write_text(declaration)
    return root


@pytest.mark.parametrize('mode,base,expected', [
    ('subscription', 'https://unrelated.example/v1', 'gpt-6.1-sol'),
    ('api_key', 'https://api.openai.com/v1', 'gpt-4.1-nano'),
    ('api_key', 'https://openrouter.ai/api/v1', 'openai/gpt-4.1-nano'),
    ('api_key', 'https://openrouter.ai/api/v1/', 'openai/gpt-4.1-nano'),
])
def test_bundled_gpt_cases_without_importing_factory_or_catalog(mode, base, expected):
    from api_agent.llm_providers.gpt import __file__ as package
    factory = Path(package).parent / 'factory.py'
    config = {'LLM_PROVIDER': str(factory)+':factory', 'GPT_AUTH_MODE': mode, 'GPT_BASE_URL': base}
    assert request_selection(config, owner_root=factory.parent) == expected


def test_first_match_relative_owner_and_scalar_or_list_conditions(tmp_path):
    root = provider(tmp_path, '''version=1
[tests]
integration=[]
request_selections=[
  {when={ENABLED=true, COUNT=[1,2]}, selected_llm="first-case"},
  {when={}, selected_llm="fallback"},
]
''')
    config = {'LLM_PROVIDER': 'provider/factory.py:create', 'ENABLED': True, 'COUNT': 2}
    assert request_selection(config, owner_root=tmp_path) == 'first-case'
    config['COUNT'] = 3
    assert request_selection(config, owner_root=tmp_path) == 'fallback'
    assert provider_integration_suites(SimpleNamespace(owner_root=tmp_path), config) == {}
    assert load_test_declaration(root)['request_selections'][0]['selected_llm'] == 'first-case'


@pytest.mark.parametrize('condition', ['absent-provider', 'absent-file', 'absent-cases', 'unmatched'])
def test_only_missing_provider_test_capability_skips(tmp_path, condition):
    root = provider(tmp_path)
    config = {'LLM_PROVIDER': str(root/'factory.py')+':create'}
    if condition == 'absent-provider':
        config.clear()
    elif condition == 'absent-file':
        (root/'cfg/tests.toml').unlink()
    elif condition == 'unmatched':
        (root/'cfg/tests.toml').write_text('version=1\n[tests]\nintegration=[]\nrequest_selections=[{when={MODE="other"},selected_llm="case"}]')
    with pytest.raises(SetupPrerequisiteError):
        request_selection(config, owner_root=tmp_path)


@pytest.mark.parametrize('rules', [
    '"not-an-array"', '[1]', '[{}]',
    '[{when={},selected_llm=2}]', '[{when={},selected_llm=""}]',
    '[{when={},selected_llm="   "}]', '[{when={},selected_llm="bad\\nname"}]',
    '[{when={},selected_llm="'+('a'*257)+'"}]',
    '[{when=1,selected_llm="case"}]', '[{when={},selected_llm="case",extra=true}]',
    '[{when={lower="x"},selected_llm="case"}]', '[{when={"BAD-KEY"="x"},selected_llm="case"}]',
    '[{when={MODE=[]},selected_llm="case"}]', '[{when={MODE=1.2},selected_llm="case"}]',
    '[{when={MODE=["x",{}]},selected_llm="case"}]',
])
def test_invalid_test_parameters_fail_instead_of_skipping(tmp_path, rules):
    root = provider(tmp_path, 'version=1\n[tests]\nintegration=[]\nrequest_selections='+rules)
    with pytest.raises(CollectionError):
        request_selection({'LLM_PROVIDER': str(root/'factory.py')+':create'}, owner_root=tmp_path)


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
