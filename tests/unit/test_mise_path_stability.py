"""Managed tool preparation must not fragment engine identities."""
from concurrent.futures import ThreadPoolExecutor
import os
import shutil

from api_agent.runtime import mise_tools
from api_agent.runtime.engine_targets import EngineTarget


def engine_key(path):
    target = EngineTarget('local', '/engine', 'a' * 64, 'b' * 64,
                          {'PATH': path, 'DOCKER_HOST': 'unix:///engine.sock'})
    return target.key


def test_repeated_preparation_preserves_search_order_and_engine_key(fake_mise, monkeypatch):
    _, bin_dir, tools, _ = fake_mise
    fallback = str(bin_dir)
    # Empty entries search the current directory and must keep their precedence.
    monkeypatch.setenv('PATH', os.pathsep.join(('', fallback, fallback, '')))

    mise_tools.ensure_tools('node', 'npm')
    first = os.environ['PATH']
    first_key = engine_key(first)
    mise_tools.ensure_tools('node', 'npm')

    assert first.split(os.pathsep) == [str(tools), '', fallback]
    assert os.environ['PATH'] == first
    assert engine_key(os.environ['PATH']) == first_key


def test_concurrent_preparations_keep_each_distinct_tool_directory(fake_mise, tmp_path, monkeypatch):
    _, bin_dir, _, _ = fake_mise
    node_dir, npm_dir = tmp_path / 'node-bin', tmp_path / 'npm-bin'
    node_dir.mkdir()
    npm_dir.mkdir()
    for executable in (node_dir / 'node', npm_dir / 'npm'):
        executable.write_text('synthetic')
        executable.chmod(0o755)
    monkeypatch.setenv('FAKE_MISE_NODE', str(node_dir / 'node'))
    monkeypatch.setenv('FAKE_MISE_NPM', str(npm_dir / 'npm'))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(mise_tools.ensure_tools, ('node', 'npm', 'node', 'npm')))

    paths = os.environ['PATH'].split(os.pathsep)
    assert paths.count(str(node_dir)) == 1
    assert paths.count(str(npm_dir)) == 1
    assert paths.count(str(bin_dir)) == 1


def test_engine_key_distinguishes_search_order():
    assert engine_key(os.pathsep.join(('/first', '/second'))) != engine_key(
        os.pathsep.join(('/second', '/first')))


def test_interleaved_preparation_keeps_key_when_selected_tools_remain_first(fake_mise, tmp_path, monkeypatch):
    node_dir, java_dir = tmp_path / 'node-bin', tmp_path / 'java-bin'
    node_dir.mkdir()
    java_dir.mkdir()
    for executable in (node_dir / 'node', java_dir / 'java'):
        executable.write_text('synthetic')
        executable.chmod(0o755)
    monkeypatch.setenv('FAKE_MISE_NODE', str(node_dir / 'node'))
    monkeypatch.setenv('FAKE_MISE_JAVA', str(java_dir / 'java'))
    mise_tools.ensure_tools('node')
    mise_tools.ensure_tools('java')
    path = os.environ['PATH']
    key = engine_key(path)

    mise_tools.ensure_tools('node')

    assert os.environ['PATH'] == path
    assert engine_key(os.environ['PATH']) == key
    assert shutil.which('node') == str(node_dir / 'node')
    assert shutil.which('java') == str(java_dir / 'java')


def test_preparation_moves_selected_executable_ahead_of_shadow(fake_mise, tmp_path, monkeypatch):
    _, bin_dir, _, _ = fake_mise
    selected, shadow = tmp_path / 'selected', tmp_path / 'shadow'
    selected.mkdir()
    shadow.mkdir()
    for executable in (selected / 'node', shadow / 'node'):
        executable.write_text('synthetic')
        executable.chmod(0o755)
    monkeypatch.setenv('FAKE_MISE_NODE', str(selected / 'node'))
    monkeypatch.setenv('PATH', os.pathsep.join((str(shadow), str(selected), str(bin_dir))))
    old_key = engine_key(os.environ['PATH'])

    mise_tools.ensure_tools('node')

    assert shutil.which('node') == str(selected / 'node')
    assert engine_key(os.environ['PATH']) != old_key
