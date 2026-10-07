"""Boundary behavior for configuration declarations, resolution, and persistence."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import compose_schema, parse_options, resolve_config, schema_from_dict
from api_agent.config.declarations import ConfigSchema, FieldDeclaration, shared_declaration_path
from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor, serialize_value
from api_agent.config.resolve import ConfigError, convert_value, format_enum_values
from api_agent.config.session import ConfigSession


def schema(fields):
    return schema_from_dict({'version': 1, 'fields': fields})


@pytest.mark.parametrize('document, message', [
    ({'version': True}, 'version'),
    ({'version': 1, 'fields': []}, 'fields table'),
    ({'version': 1, 'fields': {'lower': {'type': 'str'}}}, 'Invalid configuration field'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'extra': 1}}}, 'unknown metadata'),
    ({'version': 1, 'fields': {'X': {'type': 'path'}}}, 'explicit anchor'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'anchor': 'invalid'}}}, 'invalid path anchor'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'validation': 'invalid'}}}, 'unknown validation'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'conditional_defaults': {}}}}, 'array'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'applies_when': []}}}, 'Predicate'),
    ({'version': 1, 'fields': {'X': {'type': 'str', 'applies_when': {'any': []}}}}, 'nonempty'),
])
def test_reject_invalid_declarations(document, message):
    with pytest.raises(ValueError, match=message):
        schema_from_dict(document)


def test_predicate_array_and_conditional_value_validation():
    with pytest.raises(ValueError, match='expected int'):
        schema({'X': {'type': 'int', 'default': 'wrong'}})
    with pytest.raises(ValueError, match='expected bool'):
        schema({'X': {'type': 'bool', 'applies_when': {'X': [True, 'True']}}})
    with pytest.raises(ValueError, match='expected int'):
        schema({'X': {'type': 'int', 'conditional_defaults': [{'when': {}, 'value': 'wrong'}]}})
    with pytest.raises(ValueError, match='expected int'):
        schema({'X': {'type': 'int', 'enum': ['wrong']}})


def test_declaration_path_fallback_and_override_errors(tmp_path, monkeypatch):
    import api_agent.config.declarations as declarations
    fake = tmp_path / 'one' / 'two' / 'three' / 'declarations.py'
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(declarations, '__file__', str(fake))
    assert shared_declaration_path() == fake.parents[1] / 'cfg/config.toml'
    assert shared_declaration_path(tmp_path) == tmp_path / 'cfg/config.toml'
    shared = schema({'X': {'type': 'str'}, 'P': {'type': 'path', 'anchor': 'base_root'}})
    with pytest.raises(ValueError, match='no shared field'):
        compose_schema(shared, schema({'NEW': {'type': 'str', 'override': True}}))
    with pytest.raises(ValueError, match='Unknown default override'):
        compose_schema(shared, overrides={'NEW': 'x'})
    with pytest.raises(ValueError, match='explicit anchor'):
        compose_schema(shared, overrides={'P': {'value': 'x'}})
    changed = compose_schema(shared, overrides={'X': 'new'})
    assert changed.fields['X'].default == 'new'


def test_dotenv_rejects_malformed_assignments_and_quotes(tmp_path):
    path = tmp_path / '.env'
    path.write_text('export =oops\n')
    with pytest.raises(ValueError, match='expected KEY=value'):
        read_dotenv(path)
    path.write_text('GOOD="open\n')
    with pytest.raises(ValueError, match='unclosed quoted value'):
        read_dotenv(path)


def test_editor_detects_symlink_swap_before_replace(tmp_path, monkeypatch):
    import api_agent.config.editor as editor
    path = tmp_path / '.env'
    target = tmp_path / 'target'
    target.write_text('X=old\n')
    original = editor.os.fsync
    def swap(descriptor):
        original(descriptor)
        path.symlink_to(target)
    monkeypatch.setattr(editor.os, 'fsync', swap)
    with pytest.raises(ValueError, match='symlink'):
        DotenvEditor(path).update({'X': 'new'})
    assert target.read_text() == 'X=old\n'
    assert not list(tmp_path.glob('..env.*'))


def test_parse_option_boundaries():
    fields = schema({'FLAG': {'type': 'bool'}, 'NAME': {'type': 'str'}})
    parsed = parse_options(fields, ['-x', '--unknown', '--flag', 'False', '--name', 'value'])
    assert parsed.values == {'FLAG': 'False', 'NAME': 'value'}
    assert parsed.errors == ('Unknown option -x', 'Unknown option --unknown')
    assert parse_options(fields, ['--flag']).values['FLAG'] == 'True'
    assert parse_options(fields, ['--name']).errors == ('NAME: option requires a value',)
    assert parse_options(fields, ['-h']).values['HELP'] == 'True'


def test_conversion_and_resolution_boundaries(tmp_path):
    field = FieldDeclaration('P', 'path', anchor='base_root')
    assert convert_value(field, str(tmp_path), external=True, anchors={}) == tmp_path
    with pytest.raises(ValueError, match='absolute base_root anchor'):
        convert_value(field, 'child', external=True, anchors={'base_root': Path('relative')})
    with pytest.raises(ValueError, match='external values'):
        convert_value(field, 5, external=True, anchors={})
    assert format_enum_values(('', 'x')) == '<blank>, x'
    obj = FieldDeclaration('J', 'jsonobject')
    with pytest.raises(ValueError, match='JSON object'):
        convert_value(obj, '{', external=True, anchors=None)
    with pytest.raises(ValueError, match='expected jsonobject'):
        convert_value(obj, '[]', external=True, anchors=None)
    nested = schema({'A': {'type': 'bool', 'default': True},
                     'B': {'type': 'str', 'conditional_defaults': [
                         {'when': {'any': [{'A': False}, {'A': True}]}, 'value': 'found'}]}})
    assert resolve_config(nested, environ={}).values['B'] == 'found'
    cyclic = schema({'A': {'type': 'str', 'conditional_defaults': [{'when': {'B': 'x'}, 'value': 'x'}]},
                     'B': {'type': 'str', 'conditional_defaults': [{'when': {'A': 'x'}, 'value': 'x'}]}})
    with pytest.raises(ConfigError, match='cyclic'):
        resolve_config(cyclic, environ={})


def make_session(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    context = SimpleNamespace(base_root=base, pack_root=pack, dotenv_path=pack / '.env',
                              anchors={'base_root': base, 'pack_root': pack}, external_pack=True)
    return ConfigSession(context, schema({'HELP': {'type': 'bool', 'default': False},
                                          'X': {'type': 'str', 'default': 'base'},
                                          'HIDDEN': {'type': 'str', 'default': 'silent', 'acknowledge_default': False}}), environ={})


def test_session_restore_cleanup_and_unsaved_boundaries(tmp_path, monkeypatch):
    import api_agent.config.session as session_module
    session = make_session(tmp_path)
    path = session.context.base_root / '.env'
    path.write_text('temporary\n')
    session_module.ConfigSession._restore(path, None)
    assert not path.exists()
    path.write_text('X=old\n')
    session_module.ConfigSession._restore(path, b'X=new\n')
    assert path.read_text() == 'X=new\n'
    assert session.unset_saved('MISSING').values['X'] == 'new'
    with pytest.raises(ConfigError, match='managed'):
        session.unset_saved('UNATTENDED_AUTH')
    rows = {row.name: row for row in session.cleanup_rows()}
    assert 'HIDDEN' not in rows
    path.write_text('X=new\nHIDDEN=saved\n')
    assert 'HIDDEN' in {row.name for row in session.cleanup_rows()}
    assert session.save_defaults([]).values['X'] == 'new'
    assert serialize_value({'x': 1}) == '{"x":1}'


def test_restore_removes_temp_file_after_replace_failure(tmp_path, monkeypatch):
    import api_agent.config.session as module
    path = tmp_path / '.env'
    path.write_bytes(b'original')
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError, match='replace failed'):
        ConfigSession._restore(path, b'restored')
    assert path.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [path]


def test_unsaved_choice_and_cleanup_without_help(tmp_path):
    session = make_session(tmp_path)
    session.schema = ConfigSchema({name: field for name, field in session.schema.fields.items() if name != 'HELP'})
    assert session.saved_path('X') is None
    assert session.unset_saved('X').values['X'] == 'base'
    session.set_value('X', 'choice')
    assert session.save_defaults([]).fields['X'].source == 'interactive'
    session.delete_keys(['X'])
    assert session.result().values['X'] == 'base'
    assert 'X' in {row.name for row in session.cleanup_rows()}
