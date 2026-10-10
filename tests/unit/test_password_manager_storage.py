"""Selected-pack manager descriptors stay inert and keep the existing JSON shape."""
import json
from types import SimpleNamespace

import pytest

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor
from api_agent.config.session import ConfigSession


def _session(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    (pack / 'cfg').mkdir(parents=True)
    implementation = pack / 'manager.py'
    implementation.write_text('version = 1\n', encoding='utf-8')
    (pack / 'cfg/password_managers.toml').write_text(
        'version = 1\n[[managers]]\nid = "synthetic"\nlabel = "Synthetic Vault"\n'
        'implementation = "../manager.py"\ncli_default = "synthetic-cli"\n', encoding='utf-8')
    context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                              dotenv_path=base / '.env', external_pack=True, anchors={})
    schema = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_AUTH': {'type': 'jsonobject'}}})
    return ConfigSession(context, schema, environ={}), implementation


def test_pack_manager_save_move_and_reset_keeps_descriptor_shape(tmp_path):
    session, implementation = _session(tmp_path)
    registry = session.password_manager_registry
    assert ('Synthetic Vault', 'synthetic') in registry.choices
    assert registry.get('synthetic').implementation == implementation.resolve()
    session.save_auth_role('LLM User', [{'vault': 'work', 'user': 'first'}])
    session.save_auth_manager({'manager': 'synthetic', 'cli_path': ''})
    base, pack = session.target_paths[0], session.target_paths[-1]
    stored = json.loads(read_dotenv(base)['UNATTENDED_AUTH'])
    assert stored == {'roles': {'LLM User': [{'vault': 'work', 'user': 'first'}]},
                      'password_manager': {'manager': 'synthetic', 'cli_path': 'synthetic-cli'}}
    assert 'implementation' not in read_dotenv(base)['UNATTENDED_AUTH']
    session.switch_target(pack)
    session.save_auth_manager({'manager': 'synthetic', 'cli_path': '/custom/synthetic'})
    assert 'password_manager' not in json.loads(read_dotenv(base)['UNATTENDED_AUTH'])
    assert json.loads(read_dotenv(pack)['UNATTENDED_AUTH'])['password_manager'] == {
        'manager': 'synthetic', 'cli_path': '/custom/synthetic'}
    assert session.result(_review=True).values['UNATTENDED_AUTH']['password_manager']['manager'] == 'synthetic'
    session.reset_auth_manager()
    assert 'UNATTENDED_AUTH' not in read_dotenv(pack)


def test_inactive_pack_rejects_saved_manager_without_rewriting_it(tmp_path):
    session, _ = _session(tmp_path)
    session.save_auth_manager({'manager': 'synthetic', 'cli_path': 'synthetic-cli'})
    saved = session.dotenv_path.read_bytes()
    session.context.external_pack = False
    assert ('Synthetic Vault', 'synthetic') not in session.password_manager_registry.choices
    with pytest.raises(ValueError, match='unavailable'):
        session.save_auth_manager({'manager': 'synthetic', 'cli_path': 'synthetic-cli'})
    assert session.dotenv_path.read_bytes() == saved


@pytest.mark.parametrize('replacement', [
    {'manager': 'keeper', 'cli_path': '/custom/keeper'}, None])
def test_explicit_repair_or_reset_replaces_unavailable_manager_and_keeps_roles(tmp_path, replacement):
    session, _ = _session(tmp_path)
    session.save_auth_role('LLM User', [{'vault': 'work', 'user': 'first'}])
    session.save_auth_manager({'manager': 'synthetic', 'cli_path': 'synthetic-cli'})
    original = session.dotenv_path.read_bytes()
    session.context.external_pack = False
    assert session.result(_review=True).values['UNATTENDED_AUTH'] == {'_auth_error': 'manager_unavailable'}
    assert session.dotenv_path.read_bytes() == original
    if replacement is None:
        session.reset_auth_manager()
    else:
        session.save_auth_manager(replacement)
    stored = json.loads(read_dotenv(session.dotenv_path)['UNATTENDED_AUTH'])
    assert stored['roles'] == {'LLM User': [{'vault': 'work', 'user': 'first'}]}
    assert stored.get('password_manager') == replacement
    assert session.result(_review=True).values['UNATTENDED_AUTH']['roles'] == stored['roles']


@pytest.mark.parametrize('reset', [False, True])
def test_legacy_unavailable_manager_repair_preserves_account_descriptors(tmp_path, reset):
    session, _ = _session(tmp_path)
    legacy = {'LLM User': [{'manager': 'synthetic', 'cli_path': '/old-cli',
                            'vault': 'work', 'user': 'first'}]}
    DotenvEditor(session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps(legacy)})
    session.context.external_pack = False
    if reset:
        session.reset_auth_manager()
    else:
        session.save_auth_manager({'manager': 'keeper', 'cli_path': '/custom/keeper'})
    stored = json.loads(read_dotenv(session.dotenv_path)['UNATTENDED_AUTH'])
    assert stored['roles'] == {'LLM User': [{'vault': 'work', 'user': 'first'}]}
    assert stored.get('password_manager') == (None if reset else
        {'manager': 'keeper', 'cli_path': '/custom/keeper'})


@pytest.mark.parametrize('old', [
    {'password_manager': {'manager': 'synthetic', 'cli_path': '/old', 'extra': 'bad'}, 'roles': {}},
    {'LLM User': [{'manager': 'synthetic', 'cli_path': 7, 'vault': 'work', 'user': 'first'}]},
])
def test_explicit_repair_rejects_malformed_old_descriptor_without_writing(tmp_path, old):
    session, _ = _session(tmp_path)
    DotenvEditor(session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps(old)})
    before = session.dotenv_path.read_bytes()
    session.context.external_pack = False
    with pytest.raises(ValueError, match='require'):
        session.save_auth_manager({'manager': 'keeper', 'cli_path': '/keeper'})
    assert session.dotenv_path.read_bytes() == before
