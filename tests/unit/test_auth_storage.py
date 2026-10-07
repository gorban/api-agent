"""Canonical auth storage and migration through temporary dotenv files."""
import json
from types import SimpleNamespace

import pytest

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor
from api_agent.config.resolve import ConfigError
from api_agent.config.session import ConfigSession


def session(tmp_path, *, environ=None, argv=()):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                              dotenv_path=base / '.env', external_pack=True, anchors={})
    schema = schema_from_dict({'version': 1, 'fields': {'UNATTENDED_AUTH': {'type': 'jsonobject'}}})
    return ConfigSession(context, schema, environ=environ or {}, argv=argv)


def account(user='one'):
    return {'vault': 'vault', 'user': user}


def put(path, value):
    DotenvEditor(path).update({'UNATTENDED_AUTH': json.dumps(value, separators=(',', ':'))})


def saved(path):
    raw = read_dotenv(path).get('UNATTENDED_AUTH')
    return json.loads(raw) if raw is not None else None


def test_role_locality_merge_and_explicit_empty(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_role('LLM User', [account('llm')])
    config.save_auth_role('Secondary', [account('api')])
    assert config.dotenv_path == base
    assert saved(base) == {'roles': {'LLM User': [account('llm')]}}
    assert saved(pack) == {'roles': {'Secondary': [account('api')]}}
    effective = config.result(_review=True).values['UNATTENDED_AUTH']
    assert effective['roles'] == {'LLM User': [account('llm')], 'Secondary': [account('api')]}
    config.switch_target(pack)
    config.save_auth_role('LLM User', [])
    assert saved(base) is None
    assert saved(pack)['roles']['LLM User'] == []


def test_legacy_uniform_migrates_when_edited_and_conflict_rejected(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    put(base, {'LLM User': [{'manager': 'keeper', 'cli_path': '/keeper', **account('old')}], 'Secondary': []})
    config.save_auth_role('LLM User', [account('new')])
    assert saved(base) == {'password_manager': {'manager': 'keeper', 'cli_path': '/keeper'},
                           'roles': {'LLM User': [account('new')], 'Secondary': []}}
    before = base.read_bytes()
    put(pack, {'Secondary': [{'manager': '1password', 'cli_path': 'op', **account('x')},
                             {'manager': 'keeper', 'cli_path': 'keeper', **account('y')}]})
    with pytest.raises(ConfigError, match='choose one shared password manager'):
        config.save_auth_role('LLM User', [])
    assert base.read_bytes() == before


def test_manager_save_move_reset_and_external_override(tmp_path):
    external = json.dumps({'password_manager': {'manager': '1password', 'cli_path': 'op'}, 'roles': {'LLM User': [account('external')]}})
    config = session(tmp_path, environ={'UNATTENDED_AUTH': external})
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_role('LLM User', [account('saved')])
    config.save_auth_manager({'manager': '1password', 'cli_path': '/custom op'})
    assert saved(base)['password_manager'] == {'manager': '1password', 'cli_path': '/custom op'}
    assert config.result(_review=True).values['UNATTENDED_AUTH'] == json.loads(external)
    config.switch_target(pack)
    config.save_auth_manager({'manager': 'keeper', 'cli_path': '/new keeper'})
    assert 'password_manager' not in saved(base)
    assert saved(pack)['password_manager']['cli_path'] == '/new keeper'
    config.reset_auth_manager()
    assert saved(pack) is None
    current = config.result(_review=True).values['UNATTENDED_AUTH']
    assert current['password_manager'] == {'manager': '1password', 'cli_path': 'op'}
    assert config.environ['UNATTENDED_AUTH'] == external


def test_inactive_role_preserved_and_pack_override(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    put(base, {'roles': {'LLM User': [account('base')], 'Inactive': [{'unknown': 'kept'}]}})
    put(pack, {'roles': {'LLM User': [account('pack')]}})
    config.save_auth_role('Secondary', [])
    assert saved(base)['roles']['Inactive'] == [{'unknown': 'kept'}]
    assert config.result(_review=True).values['UNATTENDED_AUTH']['roles']['LLM User'] == [account('pack')]
    assert saved(pack)['roles']['Secondary'] == []


def test_invalid_outer_and_symlink_preflight(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    base.write_text('UNATTENDED_AUTH="bad json"\n')
    with pytest.raises(ConfigError, match='JSON object'):
        config.save_auth_role('LLM User', [])
    base.unlink()
    real = tmp_path / 'linked.env'
    put(real, {'roles': {'Secondary': [account()]}})
    pack.symlink_to(real)
    with pytest.raises(ValueError, match='symlink'):
        config.save_auth_role('Secondary', [account('new')])
    assert pack.is_symlink() and saved(real)['roles']['Secondary'] == [account()]


def test_rollback_destination_then_source_failure(tmp_path, monkeypatch):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    put(base, {'roles': {'Secondary': [account('old')]}})
    before = base.read_bytes()
    original = DotenvEditor.update
    def fail_target(self, values):
        if self.path == pack:
            raise OSError('target failure')
        return original(self, values)
    monkeypatch.setattr(DotenvEditor, 'update', fail_target)
    with pytest.raises(OSError, match='target failure'):
        config.save_auth_role('Secondary', [account('new')])
    assert base.read_bytes() == before and not pack.exists()
    monkeypatch.setattr(DotenvEditor, 'update', original)
    original_delete = DotenvEditor.delete
    def fail_after_source(self, names):
        original_delete(self, names)
        if self.path == base:
            raise OSError('source failure')
    monkeypatch.setattr(DotenvEditor, 'delete', fail_after_source)
    with pytest.raises(OSError, match='source failure'):
        config.save_auth_role('Secondary', [account('new')])
    assert base.read_bytes() == before and not pack.exists()


def test_explicit_manager_choice_resolves_mixed_legacy(tmp_path):
    config = session(tmp_path)
    base = config.target_paths[0]
    put(base, {'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('A')},
                            {'manager': '1password', 'cli_path': 'op', **account('B')}]})
    with pytest.raises(ConfigError, match='choose one shared password manager'):
        config.save_auth_role('LLM User', [account('A')])
    config.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
    assert saved(base) == {'password_manager': {'manager': '1password', 'cli_path': 'op'},
                           'roles': {'LLM User': [account('A'), account('B')]}}


def test_cross_file_legacy_conflict_and_empty_role_manager(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    put(base, {'LLM User': [{'manager': 'keeper', 'cli_path': '/keeper', **account('A')}]})
    put(pack, {'Secondary': [{'manager': '1password', 'cli_path': 'op', **account('B')}]})
    combined = config.result(_review=True).values['UNATTENDED_AUTH']
    from api_agent.auth.profiles import normalize_auth
    with pytest.raises(ValueError, match='choose one shared password manager'):
        normalize_auth(combined)
    config.switch_target(pack)
    config.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
    assert config.result(_review=True).values['UNATTENDED_AUTH']['roles'] == {
        'LLM User': [account('A')], 'Secondary': [account('B')]}
    assert saved(base) == {'roles': {'LLM User': [account('A')]}}
    assert saved(pack) == {'password_manager': {'manager': '1password', 'cli_path': 'op'},
                           'roles': {'Secondary': [account('B')]}}
    # An empty legacy array carries no manager choice and must not erase the shared one.
    put(base, {'LLM User': [{'manager': '1password', 'cli_path': 'op', **account('A')}]})
    put(pack, {'Secondary': []})
    assert config.result(_review=True).values['UNATTENDED_AUTH']['password_manager'] == {
        'manager': '1password', 'cli_path': 'op'}


def test_reset_manager_reloads_roles_after_later_role_save(tmp_path):
    external = json.dumps({'password_manager': {'manager': '1password', 'cli_path': 'op'},
                           'roles': {'LLM User': [account('outside')]}})
    config = session(tmp_path, environ={'UNATTENDED_AUTH': external})
    config.save_auth_role('Secondary', [account('first')])
    config.reset_auth_manager()
    assert config.result(_review=True).values['UNATTENDED_AUTH']['password_manager']['manager'] == '1password'
    config.save_auth_role('Secondary', [account('second')])
    # ENV supplies the roles for this invocation; the reset never freezes an old file snapshot.
    assert config.result(_review=True).values['UNATTENDED_AUTH']['roles'] == {'LLM User': [account('outside')]}
    config.save_auth_manager({'manager': '1password', 'cli_path': '/op'})
    assert config.result(_review=True).values['UNATTENDED_AUTH'] == json.loads(external)


def test_reset_manager_followed_by_role_save_refreshes_current_roles(tmp_path):
    config = session(tmp_path)
    config.save_auth_role('LLM User', [account('first')])
    config.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
    config.reset_auth_manager()
    config.save_auth_role('LLM User', [account('second')])
    effective = config.result(_review=True).values['UNATTENDED_AUTH']
    assert effective['password_manager'] == {'manager': '', 'cli_path': ''}
    assert effective['roles']['LLM User'] == [account('second')]


def test_role_save_rejects_cross_file_manager_conflict_before_writes(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    put(base, {'password_manager': {'manager': '', 'cli_path': ''},
               'roles': {'LLM User': [account('A')]}})
    put(pack, {'password_manager': {'manager': '1password', 'cli_path': 'op'},
               'roles': {'Secondary': [account('B')]}})
    before = base.read_bytes(), pack.read_bytes()
    with pytest.raises(ConfigError, match='choose one shared password manager'):
        config.save_auth_role('LLM User', [account('new')])
    assert (base.read_bytes(), pack.read_bytes()) == before
    config.save_auth_manager({'manager': '', 'cli_path': ''})
    config.save_auth_role('LLM User', [account('new')])
    assert config.result(_review=True).values['UNATTENDED_AUTH']['roles']['LLM User'] == [account('new')]


@pytest.mark.parametrize('bad', ['not json', json.dumps({'password_manager': {'manager': 'invalid', 'cli_path': 'x'}, 'roles': {}})])
def test_invalid_earlier_file_remains_invalid_after_later_valid(tmp_path, bad):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    DotenvEditor(base).update({'UNATTENDED_AUTH': bad})
    put(pack, {'roles': {'Secondary': []}})
    from api_agent.auth.profiles import normalize_auth
    with pytest.raises(ValueError, match='malformed or invalid') as error:
        normalize_auth(config.result(_review=True).values['UNATTENDED_AUTH'])
    assert bad not in str(error.value)


def test_mixed_legacy_in_one_file_reports_manager_choice(tmp_path):
    config = session(tmp_path)
    base = config.target_paths[0]
    put(base, {'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('A')},
                            {'manager': '1password', 'cli_path': 'op', **account('B')}]})
    from api_agent.auth.profiles import normalize_auth
    with pytest.raises(ValueError, match='choose one shared password manager'):
        normalize_auth(config.result(_review=True).values['UNATTENDED_AUTH'])


def test_account_draft_saves_before_manager_selection(tmp_path):
    config = session(tmp_path)
    base = config.target_paths[0]
    config.save_auth_role('LLM User', [account('draft')])
    assert saved(base) == {'roles': {'LLM User': [account('draft')]}}
    assert config.result(_review=True).values['UNATTENDED_AUTH']['password_manager'] == {
        'manager': '', 'cli_path': ''}
    config.save_auth_manager({'manager': 'keeper', 'cli_path': ''})
    assert saved(base)['password_manager'] == {'manager': 'keeper', 'cli_path': 'keeper'}


def test_auth_sources_follow_components_not_write_target(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    assert config.auth_sources() == {'password_manager': 'Not provided', 'roles': {}}
    config.save_auth_role('LLM User', [account('one'), account('two')])
    config.save_auth_role('Secondary', [account('pack')])
    config.save_auth_manager({'manager': 'keeper', 'cli_path': '/keeper'})
    expected = {'password_manager': '../base/.env',
                'roles': {'LLM User': '../base/.env', 'Secondary': '../pack/.env'}}
    assert config.auth_sources() == expected
    before = {path: path.read_bytes() for path in (base, pack)}
    config.switch_target(pack)
    assert config.auth_sources() == expected
    assert before == {path: path.read_bytes() for path in (base, pack)}
    config.save_auth_manager({'manager': 'keeper', 'cli_path': '/keeper'})
    expected['password_manager'] = '../pack/.env'
    assert config.auth_sources() == expected
    config.save_auth_role('LLM User', [])
    expected['roles']['LLM User'] = '../pack/.env'
    assert config.auth_sources() == expected
    config.reset_auth_manager()
    expected['password_manager'] = 'Not provided'
    assert config.auth_sources() == expected


@pytest.mark.parametrize('source', ('environment', 'cli', 'interactive'))
def test_auth_sources_external_override_and_reset(tmp_path, source):
    config = session(tmp_path)
    config.save_auth_role('LLM User', [account('saved')])
    config.save_auth_manager({'manager': 'keeper', 'cli_path': '/keeper'})
    value = json.dumps({'password_manager': {'manager': '1password', 'cli_path': 'op'},
                        'roles': {'LLM User': [account('external')]}})
    if source == 'environment':
        config.environ['UNATTENDED_AUTH'] = value
    elif source == 'cli':
        config.argv = (f'--unattended-auth={value}',)
    else:
        config.set_value('UNATTENDED_AUTH', value)
    assert config.auth_sources() == {'password_manager': source, 'roles': {'LLM User': source}}
    if source != 'interactive':
        config.reset_auth_manager()
        assert config.auth_sources() == {'password_manager': source, 'roles': {'LLM User': source}}


def test_auth_sources_legacy_and_pack_override_use_launcher_relative_paths(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.context.launcher_root = config.context.pack_root
    put(base, {'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('old')}],
               'Secondary': []})
    put(pack, {'roles': {'LLM User': [account('new')]}})
    assert config.auth_sources() == {'password_manager': '../base/.env',
                                     'roles': {'LLM User': '../pack/.env', 'Secondary': '../base/.env'}}


@pytest.mark.parametrize('bad,message', [
    ({'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('A')},
                   {'manager': '1password', 'cli_path': 'op', **account('B')}], 'Other': {}},
     'legacy roles must contain arrays'),
    ({'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('A')},
                   {'manager': '1password', 'cli_path': 'op', 'vault': '', 'user': 'B'}]},
     'nonempty vault and user'),
])
def test_manager_choice_does_not_repair_malformed_legacy_accounts(tmp_path, bad, message):
    config = session(tmp_path)
    base = config.target_paths[0]
    put(base, bad)
    before = base.read_bytes()
    with pytest.raises(ConfigError, match=message):
        config.save_auth_manager({'manager': 'keeper', 'cli_path': 'keeper'})
    assert base.read_bytes() == before


def test_invalid_manager_or_role_preserves_saved_accounts(tmp_path):
    config = session(tmp_path)
    config.save_auth_role('LLM User', [account('original')])
    base = config.target_paths[0]
    before = base.read_bytes()
    with pytest.raises(ConfigError, match='manager'):
        config.save_auth_manager({'manager': 'unknown', 'cli_path': 'cli'})
    with pytest.raises(ConfigError, match='nonempty'):
        config.save_auth_role('', [account('replacement')])
    assert base.read_bytes() == before


def test_manager_move_failure_restores_all_auth_files_and_session_choice(tmp_path, monkeypatch):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_role('LLM User', [account('A')])
    config.save_auth_manager({'manager': 'keeper', 'cli_path': 'keeper'})
    config.switch_target(pack)
    before = base.read_bytes()
    update = DotenvEditor.update

    def fail_after_update(editor, values):
        update(editor, values)
        if editor.path == base:
            raise OSError('source write failed')

    monkeypatch.setattr(DotenvEditor, 'update', fail_after_update)
    with pytest.raises(OSError, match='source write failed'):
        config.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
    assert base.read_bytes() == before
    assert not pack.exists()
    assert config.auth_sources()['password_manager'] == '../base/.env'


def test_manager_move_rejects_symlink_before_changing_source(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_manager({'manager': 'keeper', 'cli_path': 'keeper'})
    before = base.read_bytes()
    real = tmp_path / 'linked-auth.env'
    real.write_text('# keep\n')
    pack.symlink_to(real)
    config.switch_target(pack)
    with pytest.raises(ValueError, match='symlink'):
        config.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
    assert base.read_bytes() == before and real.read_text() == '# keep\n'


def test_invalid_external_auth_has_no_claimed_component_sources(tmp_path):
    config = session(tmp_path, environ={'UNATTENDED_AUTH': json.dumps({'_auth_error': 'invalid_profile'})})
    assert config.auth_sources() == {'password_manager': 'Not provided', 'roles': {}}
    config.reset_auth_manager()
    assert config.auth_sources() == {'password_manager': 'Not provided', 'roles': {}}


def test_manager_reset_preserves_accounts_when_auth_field_is_inactive(tmp_path):
    config = session(tmp_path)
    config.save_auth_role('LLM User', [account('kept')])
    config.save_auth_manager({'manager': 'keeper', 'cli_path': 'keeper'})
    config.schema = schema_from_dict({'version': 1, 'fields': {
        'AUTH_ENABLED': {'type': 'bool', 'default': False},
        'UNATTENDED_AUTH': {'type': 'jsonobject', 'applies_when': {'AUTH_ENABLED': True}},
    }})
    config.reset_auth_manager()
    assert 'UNATTENDED_AUTH' not in config.result().fields
    assert saved(config.dotenv_path) == {'roles': {'LLM User': [account('kept')]}}
    assert config.auth_sources()['password_manager'] == 'Not provided'
    review = config.result(_review=True)
    assert review.values['UNATTENDED_AUTH']['roles']['LLM User'] == [account('kept')]
    assert review.values['UNATTENDED_AUTH']['password_manager'] == {'manager': '', 'cli_path': ''}
