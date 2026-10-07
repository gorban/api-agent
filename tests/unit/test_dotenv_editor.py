import json
import stat

import pytest

from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor, serialize_value


def test_update_removes_duplicates_preserves_unrelated_and_literals(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# retained\nKEEP = value\nexport X=old\nX=duplicate\n# footer\n')
    literal = json.dumps({'user': 'quoted "name"', 'path': r'back\slash', 'shell': '${HOME}'})
    DotenvEditor(path).update({'X': literal, 'BOOL': serialize_value(False)})
    assert read_dotenv(path) == {'KEEP': 'value', 'X': literal, 'BOOL': 'False'}
    assert path.read_text().startswith('# retained\nKEEP = value\nX=')
    assert '# footer\n' in path.read_text()
    assert path.read_text().count('X=') == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_delete_all_occurrences_and_add_after_missing_newline(tmp_path):
    path = tmp_path / '.env'
    path.write_text('X=1\nexport X=2\n# last')
    editor = DotenvEditor(path)
    editor.delete(['X'])
    assert path.read_text() == '# last'
    editor.update({'NEW': ' $x # literal " quotes \' '})
    assert path.read_text().startswith('# last\n')
    assert read_dotenv(path)['NEW'] == ' $x # literal " quotes \' '


def test_editor_rejects_symlink_and_invalid_values_before_writing(tmp_path):
    target = tmp_path / 'target'
    target.write_text('X=before\n')
    path = tmp_path / '.env'
    path.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        DotenvEditor(path).delete(['X'])
    assert target.read_text() == 'X=before\n'
    path = tmp_path / 'new'
    for key, value in [('bad-key', 'x'), ('X', 'multi\nline'), ('X', 123)]:
        with pytest.raises(ValueError):
            DotenvEditor(path).update({key: value})
    assert not path.exists()


def test_atomic_replace_failure_retains_original_and_cleans_temporary(tmp_path, monkeypatch):
    import api_agent.config.editor as module
    path = tmp_path / '.env'
    path.write_text('X=old\n')
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError):
        DotenvEditor(path).update({'X': 'new'})
    assert path.read_text() == 'X=old\n'
    assert list(tmp_path.iterdir()) == [path]


def test_create_nested_file_and_missing_keys(tmp_path):
    editor = DotenvEditor(tmp_path / 'nested/.env')
    assert editor.keys() == set()
    editor.update({'DATA': serialize_value({'nested': [1, True]})})
    assert json.loads(read_dotenv(editor.path)['DATA']) == {'nested': [1, True]}
    assert editor.keys() == {'DATA'}
