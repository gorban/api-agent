"""Selected contributions are effective pack configuration, without execution."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import load_shared_schema, resolve_config, schema_from_dict
from api_agent.config.session import ConfigSession
from api_agent.providers.loading import bootstrap_schema, load_provider, provider_location


def configured_provider(tmp_path, text):
    root = tmp_path / 'adapter'
    root.mkdir()
    (root / 'cfg').mkdir()
    (root / 'cfg/config.toml').write_text('version = 1\n' + text)
    (root / 'provider.py').write_text("raise AssertionError('schema discovery must not execute the factory')\n")
    return root


def selected_base(location, extra=None):
    fields = dict(load_shared_schema().fields)
    fields['LLM_PROVIDER'] = replace(fields['LLM_PROVIDER'], default=location,
                                     acknowledge_default=False, default_source='pack_default')
    if extra:
        fields.update(schema_from_dict({'version': 1, 'fields': extra}, source='pack_default').fields)
    return type(load_shared_schema())(fields)


@pytest.mark.parametrize('external_pack', [False, True])
def test_gpt_state_default_displays_and_saves_the_runtime_path(tmp_path, monkeypatch, external_pack):
    from api_agent.config.dotenv import read_dotenv
    from api_agent.llm_providers.gpt import factory as gpt
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    base = tmp_path / 'base'
    base.mkdir()
    pack = tmp_path / 'pack' if external_pack else base
    pack.mkdir(exist_ok=True)
    context = SimpleNamespace(base_root=base, pack_root=pack, owner_root=pack,
                              launcher_root=pack, external_pack=external_pack,
                              dotenv_path=pack / '.env',
                              anchors={'base_root': base, 'pack_root': pack, 'owner_root': pack})
    location = str(Path(gpt.__file__).resolve()) + ':factory'
    session = ConfigSession(context, selected_base(location), environ={})
    session.set_value('GPT_APP_NAME', 'api-agent-2')
    expected = home / '.config/api-agent/gpt'
    candidates = {candidate.name: candidate for candidate in session.default_candidates()}
    assert candidates['GPT_STATE_DIR'].value == expected
    assert gpt.factory(session.result(_review=True).values, None).state_dir == expected
    session.save_defaults(['GPT_STATE_DIR'])
    assert read_dotenv(pack / '.env')['GPT_STATE_DIR'] == str(expected)
    assert session.result(_review=True).values['GPT_STATE_DIR'] == expected
    assert 'GPT_STATE_DIR' not in {c.name for c in session.default_candidates()}


def test_provider_defaults_win_and_explicit_values_remain_highest(tmp_path):
    root = configured_provider(tmp_path, '[fields.LIMIT]\ntype="int"\ndefault=7\nrequired=true\n')
    base = selected_base('adapter/provider.py:create', {'LIMIT': {'type': 'int', 'default': 2}})
    schema = bootstrap_schema(base, [], {}, {}, owner_root=tmp_path)
    assert schema.fields['LIMIT'].required
    for argv, environ, dotenv, expected, source in [
        ([], {}, {}, 7, 'pack_default'),
        ([], {}, {'LIMIT': '8'}, 8, 'dotenv'),
        ([], {'LIMIT': '9'}, {'LIMIT': '8'}, 9, 'environment'),
        (['--limit=10'], {'LIMIT': '9'}, {'LIMIT': '8'}, 10, 'cli'),
    ]:
        result = resolve_config(schema, argv=['--tui-mode=unit', *argv], environ=environ, dotenv=dotenv)
        result.raise_for_errors()
        assert result.values['LIMIT'] == expected
        assert result.fields['LIMIT'].source == source
    assert root.is_dir()


def test_provider_cannot_replace_a_field_with_an_incompatible_type(tmp_path):
    configured_provider(tmp_path, '[fields.LIMIT]\ntype="str"\ndefault="seven"\n')
    with pytest.raises(ValueError, match='override must preserve the shared type'):
        bootstrap_schema(selected_base('adapter/provider.py:create', {'LIMIT': {'type': 'int'}}),
                         [], {}, {}, owner_root=tmp_path)


def test_explicit_provider_choices_override_declared_selection(tmp_path):
    configured_provider(tmp_path, '[fields.LIMIT]\ntype="int"\ndefault=7\n')
    base = selected_base('/missing.py:create')
    selection = 'adapter/provider.py:create'
    for argv, environ, dotenv, overrides in [
        ([], {}, {'LLM_PROVIDER': selection}, None),
        ([], {'LLM_PROVIDER': selection}, {}, None),
        (['--llm-provider=' + selection], {}, {}, None),
        ([], {}, {}, {'LLM_PROVIDER': selection}),
    ]:
        assert bootstrap_schema(base, argv, environ, dotenv, overrides,
                                owner_root=tmp_path).fields['LIMIT'].default == 7
    assert bootstrap_schema(base, [], {'LLM_PROVIDER': ''}, {}, owner_root=tmp_path) is base
    assert bootstrap_schema(schema_from_dict({'version': 1}), [], {}, {}).fields == {}


def test_source_resolution_and_factory_loading_use_declared_owner(tmp_path, monkeypatch):
    root = configured_provider(tmp_path, '')
    (root / 'provider.py').write_text('class Adapter: version=1\ndef create(config, services): return Adapter()\n')
    unrelated = tmp_path / 'elsewhere'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    path, factory = provider_location('adapter/provider.py:create', owner_root=tmp_path)
    assert path == root / 'provider.py' and factory == 'create'
    owner = load_provider('adapter/provider.py:create', {}, {'owner_root': tmp_path})
    assert owner.implementation.version == 1
    import asyncio
    asyncio.run(owner.aclose())


def test_session_refreshes_selected_schema_after_save_and_rebinding(tmp_path):
    configured_provider(tmp_path, '[fields.LIMIT]\ntype="int"\ndefault=7\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
                              external_pack=False, dotenv_path=tmp_path / '.env', anchors={})
    session = ConfigSession(context, selected_base('adapter/provider.py:create'), environ={})
    assert session.result().fields['LIMIT'].source == 'pack_default'
    session.set_value('LIMIT', '8', persist=True)
    assert session.result().values['LIMIT'] == 8
    assert session.result().fields['LIMIT'].source == session._source(tmp_path / '.env')
    session.set_value('LLM_PROVIDER', '')
    assert 'LIMIT' not in session.schema.fields
    assert 'LIMIT' not in session.result().values


def test_declared_component_order_is_shared_by_help_and_editable_session(tmp_path, monkeypatch, capsys):
    from api_agent.runtime.bootstrap import main
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.config import compose_schema, load_declarations
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    (base / 'cfg').mkdir(parents=True)
    (base / 'cfg/config.toml').write_bytes((Path(__file__).parents[2] / 'cfg/config.toml').read_bytes())
    pack.mkdir()
    (pack / 'config.toml').write_text('version=1\n[fields.LIMIT]\ntype="int"\ndefault=2\n')
    (pack / 'first.toml').write_text('version=1\n[fields.LIMIT]\ntype="int"\ndefault=7\n')
    (pack / 'last.toml').write_text('version=1\n[fields.LIMIT]\ntype="int"\ndefault=9\n')
    (pack / 'pack.toml').write_text('version=1\nid="fixture"\nconfig="config.toml"\n[providers]\nconfigs=["first.toml","last.toml"]\n')
    monkeypatch.setattr('os.environ', {})
    assert main(['--help', '--limit=10'], base_root=base, resource_pack_dir=pack,
                _worktrees_verified=True) == 0
    assert 'Launch error' not in capsys.readouterr().err
    context = load_launch_context(base, pack)
    shared = load_shared_schema(base)
    schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared))
    session = ConfigSession(context, schema, environ={})
    assert session.result().values['LIMIT'] == 9
    assert session.result().fields['LIMIT'].source == 'pack_default'
    session.set_value('LIMIT', '12', persist=True)
    assert session.result().values['LIMIT'] == 12
    (pack / 'last.toml').write_text('version=1\n[fields.LIMIT]\ntype="int"\ndefault=10\n')
    session.unset_saved('LIMIT')
    assert session.result().values['LIMIT'] == 10
