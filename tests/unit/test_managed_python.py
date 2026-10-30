"""Existing environments must not bypass the mise-selected interpreter."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.runtime import dependencies


@pytest.fixture
def existing_environment(tmp_path, monkeypatch):
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv('API_AGENT_MISE', '1')
    return SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False), python


@pytest.mark.parametrize('outcome', ['different', 'unavailable', 'timeout'])
def test_mismatched_environment_is_preserved_and_not_used(existing_environment, monkeypatch, outcome):
    context, python = existing_environment
    def probe(command, **kwargs):
        assert command[:3] == [str(python), '-I', '-c']
        if outcome == 'unavailable':
            raise OSError('cannot execute')
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(stdout='/different/python\n')
    monkeypatch.setattr(dependencies.subprocess, 'run', probe)
    monkeypatch.setattr(dependencies, 'requirements', lambda *a, **k: pytest.fail('must reject before package changes'))
    monkeypatch.setattr(dependencies, '_relaunch', lambda *a, **k: pytest.fail('must not use mismatched Python'))
    with pytest.raises(OSError, match='Move that environment aside'):
        dependencies.ensure_environment(context, [])
    assert python.is_file()


def test_matching_environment_is_reused(existing_environment, monkeypatch):
    context, _ = existing_environment
    monkeypatch.setattr(dependencies.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(stdout=sys._base_executable + '\n'))
    monkeypatch.setattr(dependencies, 'requirements', lambda *a, **k: ())
    monkeypatch.setattr(dependencies, 'active_environment', lambda *a: True)
    assert dependencies.ensure_environment(context, []) is None


@pytest.mark.parametrize('broken_root', [False, True])
def test_broken_environment_is_not_passed_to_uv_clear(tmp_path, monkeypatch, broken_root):
    environment = tmp_path / '.venv'
    if broken_root:
        environment.symlink_to(tmp_path / 'missing-venv')
    else:
        (environment / 'bin').mkdir(parents=True)
        (environment / 'bin/python').symlink_to(tmp_path / 'missing-python')
        (environment / 'sentinel').write_text('preserve')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    monkeypatch.setenv('API_AGENT_MISE', '1')
    monkeypatch.setenv('UV_VENV_CLEAR', '1')
    monkeypatch.setattr(dependencies, 'requirements', lambda *a, **k: pytest.fail('must not recreate existing environment'))
    with pytest.raises(OSError, match='existing environment has not been changed'):
        dependencies.ensure_environment(context, [])
    assert environment.is_symlink() if broken_root else (environment / 'sentinel').read_text() == 'preserve'


def test_new_environment_uses_existing_creation_path(tmp_path, monkeypatch):
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    monkeypatch.setenv('API_AGENT_MISE', '1')
    monkeypatch.setattr(dependencies, 'requirements', lambda *a, **k: ())
    monkeypatch.setattr(dependencies.shutil, 'which', lambda _: '/managed/uv')
    calls = []
    def create(command, **kwargs):
        calls.append(command)
        (tmp_path / '.venv').mkdir()
    monkeypatch.setattr(dependencies.subprocess, 'run', create)
    monkeypatch.setattr(dependencies, '_relaunch', lambda *a, **k: 9)
    assert dependencies.ensure_environment(context, []) == 9
    assert calls == [['/managed/uv', 'venv', '--python', sys.executable, str(tmp_path / '.venv')]]
