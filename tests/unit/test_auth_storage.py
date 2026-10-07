"""Role-level unattended-auth persistence without credential retrieval."""
import json
from types import SimpleNamespace

import pytest

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv
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


def descriptor(user='one'):
    return {'manager': 'keeper', 'cli_path': 'keeper', 'vault': 'vault', 'user': user}


def saved(path):
    raw = read_dotenv(path).get('UNATTENDED_AUTH')
    return json.loads(raw) if raw is not None else None


def test_distinct_roles_merge_and_duplicate_moves(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_role('LLM User', [descriptor()])
    config.switch_target(pack)
    config.save_auth_role('Secondary', [descriptor('two')])
    assert saved(base) == {'LLM User': [descriptor()]}
    assert saved(pack) == {'Secondary': [descriptor('two')]}
    assert config.result(_review=True).values['UNATTENDED_AUTH'] == {
        'LLM User': [descriptor()], 'Secondary': [descriptor('two')]}
    config.save_auth_role('LLM User', [])
    assert saved(base) is None
    assert saved(pack) == {'Secondary': [descriptor('two')], 'LLM User': []}
    assert config.result(_review=True).values['UNATTENDED_AUTH']['LLM User'] == []


def test_unknown_role_payload_preserved_and_explicit_empty(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    from api_agent.config.editor import DotenvEditor
    DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({'Other': [{'unvalidated': 'kept'}]})})
    config.switch_target(pack)
    config.save_auth_role('LLM User', [])
    assert saved(base) == {'Other': [{'unvalidated': 'kept'}]}
    assert saved(pack) == {'LLM User': []}
    assert config.result(_review=True).values['UNATTENDED_AUTH']['Other'] == [{'unvalidated': 'kept'}]


def test_invalid_outer_json_rejected_without_mutation(tmp_path):
    config = session(tmp_path)
    base = config.target_paths[0]
    base.write_text('UNATTENDED_AUTH="not JSON"\n')
    before = base.read_bytes()
    with pytest.raises(ConfigError, match='JSON object'):
        config.save_auth_role('LLM User', [])
    assert base.read_bytes() == before


def test_external_precedence_and_clear_override(tmp_path):
    external = json.dumps({'LLM User': [descriptor('external')]})
    config = session(tmp_path, environ={'UNATTENDED_AUTH': external})
    config.save_auth_role('LLM User', [descriptor('saved')])
    assert config.result(_review=True).values['UNATTENDED_AUTH'] == json.loads(external)
    config.clear_auth_config()
    assert config.result(_review=True).values['UNATTENDED_AUTH'] == {}
    assert config.environ['UNATTENDED_AUTH'] == external


def test_validation_and_rollback(tmp_path, monkeypatch):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    config.save_auth_role('LLM User', [descriptor()])
    config.switch_target(pack)
    before = base.read_bytes()
    from api_agent.config.editor import DotenvEditor
    original_delete = DotenvEditor.delete
    def fail_base(self, names):
        if self.path == base:
            raise OSError('fixture write failure')
        return original_delete(self, names)
    monkeypatch.setattr(DotenvEditor, 'delete', fail_base)
    with pytest.raises(OSError, match='fixture write failure'):
        config.save_auth_role('LLM User', [descriptor('new')])
    assert base.read_bytes() == before
    assert not pack.exists()
    with pytest.raises(ConfigError, match='nonempty'):
        config.save_auth_role('LLM User', [{'manager': 'keeper', 'cli_path': '', 'vault': 'v', 'user': 'u'}])


def test_pack_role_migrates_from_base_and_pack_wins_on_read(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    from api_agent.config.editor import DotenvEditor
    DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({
        'LLM User': [descriptor('base')], 'Secondary': [descriptor('old')]})})
    DotenvEditor(pack).update({'UNATTENDED_AUTH': json.dumps({
        'LLM User': [descriptor('pack')]})})
    effective = config.result(_review=True).values['UNATTENDED_AUTH']
    assert effective['LLM User'] == [descriptor('pack')]
    assert effective['Secondary'] == [descriptor('old')]
    config.switch_target(pack)
    config.save_auth_role('Secondary', [descriptor('new')])
    assert saved(base) == {'LLM User': [descriptor('base')]}
    assert saved(pack) == {'LLM User': [descriptor('pack')], 'Secondary': [descriptor('new')]}


def test_destination_failure_before_creation_keeps_source(tmp_path, monkeypatch):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    from api_agent.config.editor import DotenvEditor
    DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({'Secondary': [descriptor('old')]})})
    before = base.read_bytes()
    config.switch_target(pack)
    from api_agent.config.editor import DotenvEditor
    original_update = DotenvEditor.update
    def fail_pack(self, values):
        if self.path == pack:
            raise OSError('destination unavailable')
        return original_update(self, values)
    monkeypatch.setattr(DotenvEditor, 'update', fail_pack)
    with pytest.raises(OSError, match='destination unavailable'):
        config.save_auth_role('Secondary', [descriptor('new')])
    assert base.read_bytes() == before
    assert not pack.exists()


def test_source_failure_after_mutation_restores_both_files(tmp_path, monkeypatch):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    from api_agent.config.editor import DotenvEditor
    DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({'Secondary': [descriptor('old')]})})
    base_before = base.read_bytes()
    pack.write_text('# keep pack\nOTHER=1\n')
    pack_before = pack.read_bytes()
    config.switch_target(pack)
    from api_agent.config.editor import DotenvEditor
    original_delete = DotenvEditor.delete
    def fail_after_delete(self, names):
        original_delete(self, names)
        if self.path == base:
            raise OSError('source delete failed afterward')
    monkeypatch.setattr(DotenvEditor, 'delete', fail_after_delete)
    with pytest.raises(OSError, match='source delete failed afterward'):
        config.save_auth_role('Secondary', [descriptor('new')])
    assert base.read_bytes() == base_before
    assert pack.read_bytes() == pack_before


def test_pack_role_uses_pack_target_even_when_base_selected(tmp_path):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    assert config.dotenv_path == base
    config.save_auth_role('Secondary', [descriptor()])
    assert config.dotenv_path == base
    assert not base.exists()
    assert saved(pack) == {'Secondary': [descriptor()]}


@pytest.mark.parametrize('linked_position', ['target', 'source'])
def test_symlink_preflight_preserves_every_file(tmp_path, linked_position):
    config = session(tmp_path)
    base, pack = config.target_paths[0], config.target_paths[-1]
    from api_agent.config.editor import DotenvEditor
    real = tmp_path / 'linked.env'
    DotenvEditor(real).update({'UNATTENDED_AUTH': json.dumps({'Secondary': [descriptor('old')]})})
    if linked_position == 'target':
        pack.symlink_to(real)
        DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({'Secondary': [descriptor('base')]})})
    else:
        base.symlink_to(real)
        DotenvEditor(pack).update({'UNATTENDED_AUTH': json.dumps({'Secondary': [descriptor('pack')]})})
    real_before = real.read_bytes()
    base_before = base.read_bytes()
    pack_before = pack.read_bytes()
    with pytest.raises(ValueError, match='symlink'):
        config.save_auth_role('Secondary', [descriptor('new')])
    assert (pack if linked_position == 'target' else base).is_symlink()
    assert real.read_bytes() == real_before
    assert base.read_bytes() == base_before
    assert pack.read_bytes() == pack_before
