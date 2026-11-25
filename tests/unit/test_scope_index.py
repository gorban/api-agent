"""A scope's journal location outlives its containers; anything unreadable means unknown."""
import json
import os
from pathlib import Path

import pytest

from api_agent.runtime import scope_index

SCOPE = 'a' * 16


def test_index_directory_lives_in_the_user_cache(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert scope_index.index_directory() == tmp_path / 'api-agent' / 'scopes'
    monkeypatch.delenv('XDG_CACHE_HOME')
    assert scope_index.index_directory() == Path.home() / '.cache' / 'api-agent' / 'scopes'


def test_a_recorded_directory_is_read_back_privately(tmp_path):
    scope_index.record(SCOPE, tmp_path / 'one')

    assert scope_index.directories(SCOPE) == (tmp_path / 'one',)
    path = scope_index.index_directory() / f'{SCOPE}.json'
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


def test_recording_appends_distinct_directories_and_ignores_repeats(tmp_path):
    scope_index.record(SCOPE, tmp_path / 'one')
    scope_index.record(SCOPE, tmp_path / 'two')
    before = (scope_index.index_directory() / f'{SCOPE}.json').stat().st_mtime_ns
    scope_index.record(SCOPE, tmp_path / 'one')

    assert scope_index.directories(SCOPE) == (tmp_path / 'one', tmp_path / 'two')
    # A repeat acquire rewrites nothing but still marks the scope as used just now.
    assert (scope_index.index_directory() / f'{SCOPE}.json').stat().st_mtime_ns >= before


def test_a_repeated_record_refreshes_the_last_use_time(tmp_path):
    scope_index.record(SCOPE, tmp_path / 'one')
    path = scope_index.index_directory() / f'{SCOPE}.json'
    os.utime(path, (1, 1))

    scope_index.record(SCOPE, tmp_path / 'one')

    assert scope_index.modified(SCOPE) > 1


def test_a_repeat_that_is_not_an_acquire_leaves_the_last_use_time_alone(tmp_path):
    scope_index.record(SCOPE, tmp_path / 'one')
    path = scope_index.index_directory() / f'{SCOPE}.json'
    os.utime(path, (1, 1))

    scope_index.record(SCOPE, tmp_path / 'one', used=False)

    assert scope_index.modified(SCOPE) == 1


def test_last_use_is_unknown_without_a_trusted_record(tmp_path):
    assert scope_index.modified(SCOPE) is None
    assert scope_index.modified('not-a-scope') is None
    scope_index.index_directory().mkdir(parents=True)
    (scope_index.index_directory() / f'{SCOPE}.json').symlink_to(tmp_path)
    assert scope_index.modified(SCOPE) is None


def test_only_the_newest_directories_are_kept(tmp_path):
    for number in range(scope_index.LIMIT + 3):
        scope_index.record(SCOPE, tmp_path / str(number))

    kept = scope_index.directories(SCOPE)

    assert len(kept) == scope_index.LIMIT and kept[0] == tmp_path / '3' and kept[-1] == tmp_path / str(scope_index.LIMIT + 2)


def test_forgetting_removes_the_record_and_tolerates_absence(tmp_path):
    scope_index.record(SCOPE, tmp_path)

    scope_index.forget(SCOPE)
    scope_index.forget(SCOPE)

    assert scope_index.directories(SCOPE) is None


@pytest.mark.parametrize('scope', ['', 'short', 'A' * 16, 'g' * 16, '../' + 'a' * 13, None, 7])
def test_only_scope_identifiers_name_records(tmp_path, scope):
    scope_index.record(scope, tmp_path)
    scope_index.forget(scope)

    assert scope_index.directories(scope) is None
    assert not scope_index.index_directory().exists()


def write(content, *, name=SCOPE):
    directory = scope_index.index_directory()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{name}.json').write_text(content if isinstance(content, str) else json.dumps(content))


@pytest.mark.parametrize('content', [
    'not json', '[]', 'null', {}, {'directories': []}, {'directories': 'x'}, {'directories': ['/ok'], 'extra': 1},
    {'directories': ['relative/path']}, {'directories': ['/a/../b']}, {'directories': [7]},
    {'directories': ['/a', '/a']}, {'directories': [f'/d{number}' for number in range(scope_index.LIMIT + 1)]}])
def test_unreadable_or_malformed_records_are_unknown(content):
    write(content)

    assert scope_index.directories(SCOPE) is None


def test_an_oversized_record_is_unknown():
    write('{"directories": ["/' + 'a' * scope_index.MAX_BYTES + '"]}')

    assert scope_index.directories(SCOPE) is None


def test_a_missing_record_is_unknown():
    assert scope_index.directories(SCOPE) is None


def test_a_symlinked_record_is_unknown(tmp_path):
    target = tmp_path / 'elsewhere.json'
    target.write_text(json.dumps({'directories': ['/ok']}))
    scope_index.index_directory().mkdir(parents=True)
    (scope_index.index_directory() / f'{SCOPE}.json').symlink_to(target)

    assert scope_index.directories(SCOPE) is None


def test_a_record_owned_by_someone_else_is_unknown(monkeypatch):
    write({'directories': ['/ok']})
    monkeypatch.setattr(os, 'getuid', lambda: os.stat(scope_index.index_directory()).st_uid + 1)

    assert scope_index.directories(SCOPE) is None


def test_recording_and_forgetting_never_raise_on_a_blocked_cache(tmp_path, monkeypatch):
    blocker = tmp_path / 'file'
    blocker.write_text('not a directory')
    monkeypatch.setattr(scope_index, 'index_directory', lambda: blocker / 'scopes')

    scope_index.record(SCOPE, tmp_path)
    scope_index.forget(SCOPE)

    assert scope_index.directories(SCOPE) is None


def test_forgetting_never_raises_when_the_record_cannot_be_removed(tmp_path):
    (scope_index.index_directory() / f'{SCOPE}.json').mkdir(parents=True)

    scope_index.forget(SCOPE)

    assert (scope_index.index_directory() / f'{SCOPE}.json').is_dir()
