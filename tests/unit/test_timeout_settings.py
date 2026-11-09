"""Build and test time bounds are declared settings whose defaults keep today's values."""
from pathlib import Path
import runpy
import subprocess

import pytest

from api_agent.config import load_shared_schema
from api_agent.config.process_settings import process_setting

ROOT = Path(__file__).resolve().parents[2]

DEFAULTS = {
    'UNIT_TEST_TIMEOUT_SECONDS': 60.0,
    'UNIT_TEST_IDLE_TIMEOUT_SECONDS': 60.0,
    'TEST_COLLECTION_TIMEOUT_SECONDS': 120.0,
    'SOURCE_FRONTEND_TIMEOUT_SECONDS': 300.0,
    'BOOTSTRAP_GIT_TIMEOUT_SECONDS': 5.0,
    'BOOTSTRAP_DEPENDENCY_PROBE_TIMEOUT_SECONDS': 20.0,
    'BOOTSTRAP_INTERPRETER_PROBE_TIMEOUT_SECONDS': 30.0,
    'QUALIFICATION_HTTP_TIMEOUT_SECONDS': 15.0,
    'QUALIFICATION_READY_TIMEOUT_SECONDS': 10.0,
    'QUALIFICATION_STREAM_TIMEOUT_SECONDS': 15.0,
    'QUALIFICATION_SHUTDOWN_TIMEOUT_SECONDS': 10.0,
    'QUALIFICATION_STOP_TIMEOUT_SECONDS': 10.0,
}


@pytest.mark.parametrize(('name', 'default'), DEFAULTS.items())
def test_timeout_is_a_positive_float_hidden_from_review(name, default):
    field = load_shared_schema().fields[name]

    assert (field.type, field.default, field.validation) == ('float', default, 'positive')
    assert field.acknowledge_default is False
    assert field.help


def test_process_setting_uses_the_declaration_until_the_environment_overrides_it():
    assert process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS', environ={}) == 5.0
    assert process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS', environ={'BOOTSTRAP_GIT_TIMEOUT_SECONDS': ''}) == 5.0
    assert process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS', environ={'BOOTSTRAP_GIT_TIMEOUT_SECONDS': '10'}) == 10.0


@pytest.mark.parametrize('value', ['0', '-1', 'soon', 'inf'])
def test_process_setting_rejects_invalid_environment_values_by_name(value):
    with pytest.raises(ValueError, match='^BOOTSTRAP_GIT_TIMEOUT_SECONDS: '):
        process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS', environ={'BOOTSTRAP_GIT_TIMEOUT_SECONDS': value})


def test_process_setting_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv('BOOTSTRAP_GIT_TIMEOUT_SECONDS', '7.5')

    assert process_setting('BOOTSTRAP_GIT_TIMEOUT_SECONDS') == 7.5


def _captured_timeouts(monkeypatch, module, result):
    timeouts = []
    def run(*args, **kwargs):
        timeouts.append(kwargs.get('timeout'))
        return result
    monkeypatch.setattr(module.subprocess, 'run', run)
    return timeouts


def test_git_probes_use_the_bootstrap_git_timeout(monkeypatch, tmp_path):
    from api_agent.runtime import hook_assurance, worktrees
    monkeypatch.setenv('BOOTSTRAP_GIT_TIMEOUT_SECONDS', '10')
    hook = _captured_timeouts(monkeypatch, hook_assurance, subprocess.CompletedProcess([], 0, b'', b''))
    hook_assurance._git(tmp_path, 'rev-parse')
    gate = _captured_timeouts(monkeypatch, worktrees, subprocess.CompletedProcess([], 0, b'', b''))
    worktrees._git(tmp_path, 'rev-parse')

    assert hook + gate == [10.0, 10.0]


def test_dependency_probes_use_their_bootstrap_timeouts(monkeypatch, tmp_path):
    from api_agent.runtime import dependencies
    monkeypatch.setenv('BOOTSTRAP_DEPENDENCY_PROBE_TIMEOUT_SECONDS', '40')
    monkeypatch.setenv('BOOTSTRAP_INTERPRETER_PROBE_TIMEOUT_SECONDS', '60')
    timeouts = _captured_timeouts(monkeypatch, dependencies, subprocess.CompletedProcess([], 0, '[]', ''))
    dependencies._installed_requirements(tmp_path / 'python', ['example'])
    dependencies._requirement_parser_status(tmp_path / 'python')
    dependencies._interpreter_base_executable(tmp_path / 'python')

    assert timeouts == [40.0, 40.0, 60.0]


def test_qualifier_bounds_come_from_their_settings(monkeypatch):
    for name, value in (('QUALIFICATION_HTTP_TIMEOUT_SECONDS', '30'), ('QUALIFICATION_READY_TIMEOUT_SECONDS', '20'),
                        ('QUALIFICATION_STREAM_TIMEOUT_SECONDS', '30'), ('QUALIFICATION_SHUTDOWN_TIMEOUT_SECONDS', '20'),
                        ('QUALIFICATION_STOP_TIMEOUT_SECONDS', '20')):
        monkeypatch.setenv(name, value)
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))

    assert module['timeouts']() == {'http': 30.0, 'ready': 20.0, 'stream': 30.0, 'shutdown': 20.0, 'stop': 20.0}


def test_qualifier_bounds_default_to_their_declarations(monkeypatch):
    for name in DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))

    assert module['timeouts']() == {'http': 15.0, 'ready': 10.0, 'stream': 15.0, 'shutdown': 10.0, 'stop': 10.0}
