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
    assert session.result().fields['VALUE'].source == 'dotenv'
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
