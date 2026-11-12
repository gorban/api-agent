"""Packs declare the caches they persist so they can be reviewed and purged."""
from pathlib import Path

import pytest

from api_agent.runtime import persisted_caches as subject
from api_agent.runtime.persisted_caches import PersistedCache, parse_persisted_caches, user_cache_directory
from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context

from test_pack_composition import pack

ENTRY = {'id': 'sign-in', 'label': 'Cached sign-in', 'user_cache_dir': 'fixture-auth'}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    return tmp_path


def test_user_cache_directory_follows_the_platform_convention(home, monkeypatch):
    monkeypatch.setattr(subject.sys, 'platform', 'darwin')
    assert user_cache_directory('fixture') == home / 'Library' / 'Caches' / 'fixture'
    monkeypatch.setattr(subject.sys, 'platform', 'linux')
    assert user_cache_directory('fixture') == home / '.cache' / 'fixture'
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / 'xdg'))
    assert user_cache_directory('fixture') == home / 'xdg' / 'fixture'


def test_purge_removes_the_whole_cache_and_reports_what_it_holds(home):
    cache = PersistedCache(**ENTRY)
    assert not cache.present() and cache.key == 'cache:sign-in'
    cache.purge()
    (cache.directory / 'tokens').mkdir(parents=True)
    (cache.directory / 'tokens' / 'a.json').write_text('12345')
    (cache.directory / 'tokens' / 'link').symlink_to(home)
    assert cache.present()
    assert cache.summary() == f'Cached sign-in ({cache.directory}, 5 bytes)'
    cache.purge()
    assert not cache.present() and home.exists()


def test_purging_a_symlinked_cache_removes_only_the_link(home):
    cache = PersistedCache(**ENTRY)
    elsewhere = home / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'kept').write_text('x')
    cache.directory.parent.mkdir(parents=True, exist_ok=True)
    cache.directory.symlink_to(elsewhere)
    assert cache.present() and cache.summary().endswith(', 0 bytes)')
    cache.purge()
    assert not cache.present() and (elsewhere / 'kept').read_text() == 'x'


def test_valid_declarations_are_parsed_in_order():
    other = {'id': 'other', 'label': 'Other', 'user_cache_dir': 'other.cache_1'}
    assert parse_persisted_caches([ENTRY, other]) == (PersistedCache(**ENTRY), PersistedCache(**other))
    assert parse_persisted_caches([]) == ()


@pytest.mark.parametrize('declared', [
    'sign-in', {'id': 'x'}, [1], [{**ENTRY, 'extra': 'x'}], [{**ENTRY, 'id': 1}],
    [{**ENTRY, 'id': 'Bad Id'}], [{**ENTRY, 'label': ' '}], [{**ENTRY, 'label': 'two\nlines'}],
    [{**ENTRY, 'user_cache_dir': '../escape'}], [{**ENTRY, 'user_cache_dir': 'a/b'}],
    [{**ENTRY, 'user_cache_dir': '.'}], [{**ENTRY, 'user_cache_dir': ''}],
    [ENTRY, {**ENTRY, 'user_cache_dir': 'second'}],
])
def test_invalid_declarations_are_rejected(declared):
    with pytest.raises(ResourcePackError, match='persisted_caches'):
        parse_persisted_caches(declared)


def test_manifest_declares_persisted_caches_and_layers_compose(tmp_path):
    declaration = ('[[persisted_caches]]\nid="sign-in"\nlabel="Cached sign-in"\n'
                   'user_cache_dir="fixture-auth"\n')
    first = pack(tmp_path, 'first', manifest=declaration)
    second = pack(tmp_path, 'second', manifest=declaration.replace('sign-in', 'other').replace(
        'fixture-auth', 'other-auth'))
    assert load_launch_context(tmp_path, first).persisted_caches == (PersistedCache(**ENTRY),)
    composed = load_launch_context(tmp_path, [first, second])
    assert [cache.id for cache in composed.persisted_caches] == ['sign-in', 'other']
    assert load_launch_context(tmp_path, pack(tmp_path, 'plain')).persisted_caches == ()
