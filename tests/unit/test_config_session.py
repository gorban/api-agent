from types import SimpleNamespace

import pytest

from api_agent.config import ConfigError, schema_from_dict
from api_agent.config.dotenv import read_dotenv
from api_agent.config.session import ConfigSession


@pytest.fixture
def session(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir()
    pack.mkdir()
    context = SimpleNamespace(base_root=base, pack_root=pack, dotenv_path=pack / '.env',
                              anchors={'base_root': base, 'pack_root': pack, 'owner_root': pack},
                              external_pack=True)
    schema = schema_from_dict({'version': 1, 'fields': {
        'TUI_MODE': {'type': 'str', 'required': True},
        'VALUE': {'type': 'int', 'default': 1},
        'HELP': {'type': 'bool', 'default': False},
        'DEBUG': {'type': 'bool', 'default': False, 'acknowledge_default': False},
        'TRANSIENT': {'type': 'str', 'default': 'once', 'persist_default': False},
        'DETAIL': {'type': 'str', 'default': 'low', 'conditional_defaults': [
            {'when': {'TUI_MODE': 'unit'}, 'value': 'high'}]},
        'REQUIRED': {'type': 'str', 'required_when': {'TUI_MODE': 'run'}},
        'UNATTENDED_AUTH': {'type': 'jsonobject', 'default': {}},
    }})
    return ConfigSession(context, schema, environ={})


def test_original_precedence_and_session_validation(session):
    (session.context.base_root / '.env').write_text('VALUE=2\n')
    session.dotenv_path.write_text('VALUE=3\n')
    session.environ['VALUE'] = '4'
    session.argv = ('--value=5', 'selector')
    assert session.result().values['VALUE'] == 5
    with pytest.raises(ConfigError):
        session.set_value('VALUE', 'bad')
    assert session.result().values['VALUE'] == 5
    with pytest.raises(ConfigError, match='Unknown'):
        session.set_value('UNKNOWN', 'x')
    result = session.set_value('VALUE', '6')
    assert result.fields['VALUE'].source == 'interactive'
    assert result.fields['VALUE'].explicit
    assert result.positionals == ('selector',)
    assert session.argv == ('--value=5', 'selector')
    assert session.environ == {'VALUE': '4'}


def test_choices_recompute_conditional_defaults_and_missing(session):
    assert session.result().missing == ('TUI_MODE',)
    session.set_value('TUI_MODE', 'unit')
    assert session.result().values['DETAIL'] == 'high'
    session.set_value('TUI_MODE', 'run')
    assert session.result().missing == ('REQUIRED',)
    session.set_value('REQUIRED', 'ok', persist=True)
    assert session.result().missing == ()
    assert read_dotenv(session.dotenv_path)['REQUIRED'] == 'ok'


def test_candidate_metadata_and_default_saving(session):
    candidates = {row.name: row for row in session.default_candidates()}
    assert set(candidates) == {'VALUE', 'DETAIL', 'TRANSIENT'}
    assert candidates['VALUE'].value == 1
    session.save_defaults(['VALUE', 'DETAIL'])
    assert session.result().fields['VALUE'].source == '../pack/.env'
    assert 'VALUE' not in {row.name for row in session.default_candidates()}
    for name in ('DEBUG', 'TRANSIENT', 'HELP', 'VALUE'):
        with pytest.raises(ConfigError):
            session.save_defaults([name])


def test_cleanup_presence_overrides_duplicates_and_fallback(session):
    (session.context.base_root / '.env').write_text('VALUE=2\n')
    session.dotenv_path.write_text('# keep\nVALUE=3\nVALUE=4\nUNATTENDED_AUTH={}\n')
    session.environ['VALUE'] = '5'
    rows = {row.name: row for row in session.cleanup_rows()}
    assert rows['VALUE'].value == 5 and rows['VALUE'].source == 'environment'
    assert rows['VALUE'].base_present and rows['VALUE'].pack_present and rows['VALUE'].present
    assert not rows['DETAIL'].present
    assert 'UNATTENDED_AUTH' not in rows
    session.delete_keys(['VALUE'])
    assert session.result().values['VALUE'] == 5
    session.environ.clear()
    assert session.result().values['VALUE'] == 2
    assert session.dotenv_path.read_text() == '# keep\nUNATTENDED_AUTH={}\n'
    assert session.dotenv_keys() == {'UNATTENDED_AUTH'}
    with pytest.raises(ConfigError):
        session.delete_keys(['UNATTENDED_AUTH'])


def test_choices_inserted_before_positional_separator(session):
    session.argv = ('--', '--literal-selector')
    result = session.set_value('VALUE', '7')
    assert result.values['VALUE'] == 7
    assert result.positionals == ('--literal-selector',)


def test_cleanup_includes_effective_inactive_fields_without_changing_applicability(session):
    from dataclasses import replace
    field = session.schema.fields['DETAIL']
    session.schema.fields['DETAIL'] = replace(field, applies_when={'TUI_MODE': 'unit'})
    session.set_value('TUI_MODE', 'run')
    assert 'DETAIL' not in session.result().fields
    row = next(row for row in session.cleanup_rows() if row.name == 'DETAIL')
    assert row.value == 'low' and row.source == 'shared_default'
    session.set_value('HELP', 'True')
    rows = {row.name: row for row in session.cleanup_rows()}
    assert rows['HELP'].value is True
    assert rows['DETAIL'].value == 'low'


def test_switch_target_keeps_pack_precedence_and_tracks_origins(session):
    base, pack = session.target_paths
    base.write_text('VALUE=2\n')
    pack.write_text('VALUE=3\n')
    session.switch_target(base)
    assert session.result().values['VALUE'] == 3
    assert session.result().fields['VALUE'].source == '../pack/.env'
    session.delete_keys(['VALUE'])
    assert session.result().values['VALUE'] == 3
    rows = {row.name: row for row in session.cleanup_rows()}
    assert not rows['VALUE'].base_present
    assert rows['VALUE'].pack_present
    assert not rows['VALUE'].present
    with pytest.raises(ConfigError, match='Unknown dotenv target'):
        session.switch_target(base.parent / 'other.env')


def test_persistence_moves_duplicates_and_clears_interactive_override(session):
    base, pack = session.target_paths
    base.write_text('# base\nVALUE=2\nVALUE=3\nDETAIL=keep\n')
    pack.write_text('# pack\nVALUE=4\n')
    session.set_value('VALUE', '8')
    session.environ['VALUE'] = '7'
    result = session.set_value('VALUE', '9', persist=True)
    assert result.values['VALUE'] == 7
    assert result.fields['VALUE'].source == 'environment'
    assert base.read_text() == '# base\nDETAIL=keep\n'
    session.environ.clear()
    assert session.result().fields['VALUE'].source == '../pack/.env'
    session.switch_target(base)
    result = session.set_value('VALUE', '10', persist=True)
    assert 'VALUE' not in read_dotenv(pack)
    assert result.values['VALUE'] == 10
    assert result.fields['VALUE'].source == '../base/.env'
    session.argv = ('--value=11',)
    assert session.set_value('VALUE', '12', persist=True).values['VALUE'] == 11


def test_move_failure_restores_destination_without_losing_source(session, monkeypatch):
    from api_agent.config.session import DotenvEditor
    base, pack = session.target_paths
    base.write_text('VALUE=2\n')
    pack.write_text('# original\nDETAIL=existing\n')
    original = DotenvEditor.delete

    def fail_delete(editor, names):
        if editor.path == base:
            assert read_dotenv(pack)['VALUE'] == '8'
            raise OSError('cannot remove source')
        original(editor, names)

    monkeypatch.setattr(DotenvEditor, 'delete', fail_delete)
    with pytest.raises(OSError, match='cannot remove source'):
        session.set_value('VALUE', '8', persist=True)
    assert base.read_text() == 'VALUE=2\n'
    assert pack.read_text() == '# original\nDETAIL=existing\n'


def test_default_save_uses_selected_target_and_launcher_relative_source(session):
    base, pack = session.target_paths
    session.context.launcher_root = pack.parent.parent / 'launcher'
    session.switch_target(base)
    result = session.save_defaults(['VALUE'])
    assert read_dotenv(base)['VALUE'] == '1'
    assert not pack.exists()
    assert result.fields['VALUE'].source == '../base/.env'


def test_new_destination_removed_after_failed_move(session, monkeypatch):
    from api_agent.config.session import DotenvEditor
    base, pack = session.target_paths
    base.write_text('VALUE=2\n')
    monkeypatch.setattr(DotenvEditor, 'delete', lambda *args: (_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(OSError):
        session.set_value('VALUE', '8', persist=True)
    assert not pack.exists()
    assert read_dotenv(base)['VALUE'] == '2'


def test_launcher_selects_initial_target_and_switch_toggles(session):
    session.context.launcher_root = session.context.base_root
    launched = ConfigSession(session.context, session.schema, environ={})
    assert launched.dotenv_path == session.context.base_root / '.env'
    launched.switch_target()
    assert launched.dotenv_path == session.context.pack_root / '.env'
    launched.switch_target()
    assert launched.dotenv_path == session.context.base_root / '.env'


def test_distinct_launcher_and_active_pack_are_all_readable_targets(session):
    launcher = session.context.base_root.parent / 'launcher'
    launcher.mkdir()
    session.context.launcher_root = launcher
    launched = ConfigSession(session.context, session.schema, environ={})
    base, origin, active = launched.target_paths
    base.write_text('VALUE=2\n')
    origin.write_text('VALUE=3\nDETAIL=origin\n')
    active.write_text('VALUE=4\n')
    assert launched.dotenv_path == origin
    assert launched.result().fields['VALUE'].source == '../pack/.env'
    assert launched.result().fields['DETAIL'].source == '../launcher/.env'
    launched.switch_target()
    assert launched.dotenv_path == active
    launched.switch_target(origin)
    result = launched.set_value('VALUE', '8', persist=True)
    assert result.values['VALUE'] == 8
    assert result.fields['VALUE'].source == '../launcher/.env'
    assert 'VALUE' not in read_dotenv(base)
    assert 'VALUE' not in read_dotenv(active)


def test_second_source_failure_restores_all_previous_files(session, monkeypatch):
    from api_agent.config.session import DotenvEditor
    launcher = session.context.base_root.parent / 'launcher'
    launcher.mkdir()
    session.context.launcher_root = launcher
    launched = ConfigSession(session.context, session.schema, environ={})
    base, origin, active = launched.target_paths
    base.write_text('# base\nVALUE=2\n')
    origin.write_text('# origin\nVALUE=3\n')
    active.write_text('# active\nVALUE=4\n')
    previous = {path: path.read_bytes() for path in launched.target_paths}
    delete = DotenvEditor.delete

    def fail_second(editor, names):
        if editor.path == active:
            assert 'VALUE' not in read_dotenv(base)
            assert read_dotenv(origin)['VALUE'] == '8'
            raise OSError('second source failed')
        delete(editor, names)

    monkeypatch.setattr(DotenvEditor, 'delete', fail_second)
    with pytest.raises(OSError, match='second source failed'):
        launched.set_value('VALUE', '8', persist=True)
    assert all(path.read_bytes() == content for path, content in previous.items())


def test_unset_source_independent_of_write_target_reveals_fallback(session):
    base, pack = session.target_paths
    base.write_text('VALUE=2\n')
    pack.write_text('VALUE=8\n')
    session.switch_target(base)
    assert session.saved_path('VALUE') == pack
    result = session.unset_saved('VALUE')
    assert result.values['VALUE'] == 2
    assert result.fields['VALUE'].source == '../base/.env'
    assert session.dotenv_path == base
    assert 'VALUE' not in read_dotenv(pack)
    session.switch_target(pack)
    session.unset_saved('VALUE')
    assert 'VALUE' not in read_dotenv(base)
    assert session.saved_path('VALUE') is None
    assert session.dotenv_path == pack


def test_default_review_ignores_mode_applicability_but_preserves_pack_scope(session):
    from dataclasses import replace
    session.schema.fields['DETAIL'] = replace(session.schema.fields['DETAIL'], applies_when={'TUI_MODE': 'unit'})
    session.schema.fields['VALUE'] = replace(session.schema.fields['VALUE'], pack_only=True)
    session.context.external_pack = False
    session.switch_target(session.context.base_root / '.env')
    candidates = {row.name: row for row in session.default_candidates()}
    assert candidates['DETAIL'].value == 'low'
    assert 'VALUE' not in candidates
    assert 'DETAIL' not in session.result().fields
    session.save_defaults(['DETAIL'])
    assert read_dotenv(session.dotenv_path)['DETAIL'] == 'low'
    assert 'DETAIL' not in session.result().fields
    assert 'DETAIL' not in {row.name for row in session.default_candidates()}


def test_resource_pack_token_is_reviewed_before_mode_selection(session):
    from pathlib import Path
    from api_agent.config import compose_schema, load_declarations, load_shared_schema
    base = Path(__file__).resolve().parents[2]
    shared = load_shared_schema(base)
    from api_agent.config import schema_from_dict
    pack = schema_from_dict({'version': 1, 'fields': {'PACK_TEST_TOKEN': {
        'type': 'str', 'required': True, 'applies_when': {
            'TUI_MODE': ['integration', 'e2e'], 'COLLECT_ONLY': False}}}},
        base_schema=shared, source='pack_default')
    pack_session = ConfigSession(session.context, compose_schema(shared, pack), environ={})
    assert 'PACK_TEST_TOKEN' in pack_session.result(_review=True).missing
    assert 'PACK_TEST_TOKEN' not in pack_session.result().missing
    demo = ConfigSession(session.context, shared, environ={})
    assert 'PACK_TEST_TOKEN' not in demo.result(_review=True).missing
