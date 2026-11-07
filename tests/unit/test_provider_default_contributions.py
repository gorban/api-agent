"""Exact-provider pack defaults are inert, typed and opt-in."""
import pytest
from api_agent.config import load_shared_schema, resolve_config
from api_agent.providers.loading import bootstrap_schema


@pytest.fixture
def provider_defaults(tmp_path):
    provider = tmp_path / 'llm/factory.py'
    provider.parent.mkdir()
    provider.write_text('raise AssertionError("schema discovery must not import providers")\n')
    cfg = provider.parent / 'cfg/config.toml'
    cfg.parent.mkdir()
    cfg.write_text('''version=1
[fields.LLM_DEFAULT_MODEL]
type="str"
override=true
default="provider-model"
''')
    overlay = tmp_path / 'pack/cfg/defaults.toml'
    overlay.parent.mkdir(parents=True)
    overlay.write_text('''version=1
[[provider_defaults]]
provider="../../llm/factory.py:factory"
[provider_defaults.settings]
LLM_DEFAULT_MODEL="pack-model"
LLM_ALLOWED_MODELS="pack-.+"
LLM_REASONING_EFFORT=""
''')
    return provider, overlay


def test_exact_provider_pack_defaults_override_only_defaults(provider_defaults):
    provider, overlay = provider_defaults
    config = {'LLM_PROVIDER': str(provider) + ':factory'}
    schema = bootstrap_schema(load_shared_schema(), [], {}, {}, config,
                              configuration_paths=(overlay,), owner_root=overlay.parent.parent)
    result = resolve_config(schema, environ={}, dotenv={}, overrides=config)
    assert result.values['LLM_DEFAULT_MODEL'] == 'pack-model'
    assert result.values['LLM_ALLOWED_MODELS'] == 'pack-.+'
    assert result.values['LLM_REASONING_EFFORT'] == ''
    assert result.fields['LLM_DEFAULT_MODEL'].source == 'pack_default'


@pytest.mark.parametrize('channel', ['dotenv', 'environment', 'cli', 'override'])
@pytest.mark.parametrize('value', ['', 'explicit-model'])
def test_explicit_model_controls_win(provider_defaults, channel, value):
    provider, overlay = provider_defaults
    config = {'LLM_PROVIDER': str(provider) + ':factory'}
    argv = ['--llm-default-model', value] if channel == 'cli' else []
    environ = {'LLM_DEFAULT_MODEL': value} if channel == 'environment' else {}
    dotenv = {'LLM_DEFAULT_MODEL': value} if channel == 'dotenv' else {}
    overrides = {**config, **({'LLM_DEFAULT_MODEL': value} if channel == 'override' else {})}
    schema = bootstrap_schema(load_shared_schema(), argv, environ, dotenv, overrides,
                              configuration_paths=(overlay,), owner_root=overlay.parent.parent)
    result = resolve_config(schema, argv=argv, environ=environ, dotenv=dotenv, overrides=overrides)
    if value:
        assert result.values['LLM_DEFAULT_MODEL'] == value
        assert result.fields['LLM_DEFAULT_MODEL'].explicit
    else:
        assert 'LLM_DEFAULT_MODEL' not in result.values
        assert any('LLM_DEFAULT_MODEL' in error for error in result.errors)


def test_unselected_provider_and_bare_public_defaults_are_unchanged(provider_defaults):
    provider, overlay = provider_defaults
    config = {'LLM_PROVIDER': str(provider) + ':other_factory'}
    bare = bootstrap_schema(load_shared_schema(), [], {}, {}, config)
    scoped = bootstrap_schema(load_shared_schema(), [], {}, {}, config, configuration_paths=(overlay,))
    assert scoped.fields['LLM_DEFAULT_MODEL'].default == bare.fields['LLM_DEFAULT_MODEL'].default == 'provider-model'


@pytest.mark.parametrize('text', [
    'provider_defaults={}',
    'provider_defaults=[' + ','.join('{}' for _ in range(17)) + ']',
    '[[provider_defaults]]\nprovider="../../llm/factory.py:factory"\nsettings={LLM_PROVIDER="bad"}',
    '[[provider_defaults]]\nprovider="../../llm/factory.py:factory"\nsettings={LLM_DEFAULT_MODEL=123}',
    '[[provider_defaults]]\nprovider="../../llm/factory.py:factory"\nsettings={LLM_DEFAULT_MODEL="["}',
    '[[provider_defaults]]\nprovider="../../llm/factory.py:factory"\nsettings={}',
])
def test_invalid_provider_defaults_fail_before_provider_import(provider_defaults, text):
    provider, overlay = provider_defaults
    overlay.write_text('version=1\n' + text)
    with pytest.raises(ValueError):
        bootstrap_schema(load_shared_schema(), [], {}, {}, {'LLM_PROVIDER': str(provider) + ':factory'},
                         configuration_paths=(overlay,))


def test_provider_defaults_require_discovery(provider_defaults):
    from api_agent.providers.loading import contribution_schema
    _, overlay = provider_defaults
    with pytest.raises(ValueError, match='require provider discovery'):
        contribution_schema(overlay, load_shared_schema())


@pytest.mark.parametrize('kind', ['unknown', 'sensitive'])
def test_provider_defaults_reject_missing_or_sensitive_controls(provider_defaults, kind):
    from dataclasses import replace
    from api_agent.config import ConfigSchema
    provider, overlay = provider_defaults
    shared = load_shared_schema()
    fields = dict(shared.fields)
    if kind == 'unknown':
        del fields['LLM_ALLOWED_MODELS']
    else:
        fields['LLM_ALLOWED_MODELS'] = replace(fields['LLM_ALLOWED_MODELS'], sensitive=True)
    with pytest.raises(ValueError, match='Unknown or sensitive provider default'):
        bootstrap_schema(ConfigSchema(fields), [], {}, {}, {'LLM_PROVIDER': str(provider) + ':factory'},
                         configuration_paths=(overlay,))


def test_empty_provider_defaults_is_an_inert_declaration(provider_defaults):
    provider, overlay = provider_defaults
    overlay.write_text('version=1\nprovider_defaults=[]\n')
    schema = bootstrap_schema(load_shared_schema(), [], {}, {}, {'LLM_PROVIDER': str(provider) + ':factory'},
                              configuration_paths=(overlay,))
    assert schema.fields['LLM_DEFAULT_MODEL'].default == 'provider-model'


def test_captured_provider_defaults_retain_declaration_owner_across_cwd(provider_defaults, tmp_path, monkeypatch):
    provider, overlay = provider_defaults
    admitted = overlay.read_bytes()
    overlay.write_text('invalid live file\n')
    other = tmp_path / 'elsewhere'
    other.mkdir()
    monkeypatch.chdir(other)
    schema = bootstrap_schema(load_shared_schema(), [], {}, {}, {'LLM_PROVIDER': str(provider) + ':factory'},
        configuration_paths=(overlay,), configuration_bodies={overlay: admitted}, owner_root=other)
    assert schema.fields['LLM_DEFAULT_MODEL'].default == 'pack-model'
    assert schema.fields['LLM_DEFAULT_MODEL'].contribution_origin == overlay


def test_dormant_provider_defaults_do_not_validate_against_other_provider(provider_defaults):
    provider, overlay = provider_defaults
    overlay.write_text(overlay.read_text().replace('LLM_DEFAULT_MODEL="pack-model"', 'LLM_DEFAULT_MODEL=123'))
    schema = bootstrap_schema(load_shared_schema(), [], {}, {}, {'LLM_PROVIDER': str(provider) + ':other'},
                              configuration_paths=(overlay,))
    assert schema.fields['LLM_DEFAULT_MODEL'].default == 'provider-model'
