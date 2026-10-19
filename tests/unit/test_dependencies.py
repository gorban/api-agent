"""Local tool setup tests; no package download or real venv is performed."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from api_agent.runtime.dependencies import ensure_environment, requirements, _installed_requirements


def test_requirements_merge_without_source_install(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["textual>=8,<9"]\n')
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["api-agent==0.1.0", "other>=1"]\n')
    context = SimpleNamespace(base_root=base, pack_root=pack)
    assert requirements(context, collect=True) == ('textual>=8,<9', 'other>=1', 'pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4')


def test_prepare_once_reexec_and_preserve_arguments(tmp_path):
    base = tmp_path / 'base'; base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["textual>=8,<9"]\n')
    environment = base / '.venv'; (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    context = SimpleNamespace(base_root=base, pack_root=base, external_pack=False)
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), patch('api_agent.runtime.dependencies.subprocess.run') as install, patch('api_agent.runtime.dependencies._installed_requirements', return_value=()), patch('api_agent.runtime.dependencies.subprocess.call', return_value=3) as launch:
        assert ensure_environment(context, ['--tui-mode=config-cleanup']) == 3
        assert install.call_count == 0
        assert '_worktrees_verified=True' in launch.call_args.args[0][2]
        assert launch.call_args.args[0][-1] == '--tui-mode=config-cleanup'
        assert launch.call_args.args[0][-2] == str(base)
        assert '--_launcher-root' in launch.call_args.args[0]
        assert ensure_environment(context, ['--tui-mode=config-cleanup']) == 3
        assert install.call_count == 0
    with patch('api_agent.runtime.dependencies.sys.prefix', str(environment)), patch('api_agent.runtime.dependencies._installed_requirements', return_value=()), patch('api_agent.runtime.dependencies.subprocess.call') as launch:
        assert ensure_environment(context, []) is None
        launch.assert_not_called()


def test_venv_restart_preserves_implicit_demo_selection(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=[]\n')
    environment = base / '.venv'
    (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    context = SimpleNamespace(base_root=base, pack_root=base / 'demo',
                              launcher_root=base, external_pack=False, show_pack_bootstrap=True)
    with patch('api_agent.runtime.dependencies.subprocess.call', return_value=0) as launch:
        ensure_environment(context, [])
        command = launch.call_args.args[0]
        assert '--resource-pack-dir' not in command
        assert command[-2:] == ['--_launcher-root', str(base)]
        context.show_pack_bootstrap = False
        ensure_environment(context, [])
        assert '--resource-pack-dir' in launch.call_args.args[0]


def test_optional_groups_merge_from_base_and_pack(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["base>=1"]\n[project.optional-dependencies]\nbrowser=["playwright>=1.63,<2"]\n')
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["pack>=1"]\n[project.optional-dependencies]\nbrowser=["addon>=2"]\n')
    context = SimpleNamespace(base_root=base, pack_root=pack)
    assert requirements(context) == ('base>=1', 'pack>=1')
    assert requirements(context, groups=('browser',)) == ('base>=1', 'playwright>=1.63,<2', 'pack>=1', 'addon>=2')
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["pack>=1"]\n')
    assert requirements(context, groups=('browser',)) == ('base>=1', 'playwright>=1.63,<2', 'pack>=1')


def test_removed_package_forces_reinstall_with_valid_marker(tmp_path):
    base = tmp_path / 'base'; base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["textual>=8,<9"]\n')
    environment = base / '.venv'; (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    context = SimpleNamespace(base_root=base, pack_root=base, external_pack=False)
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), patch('api_agent.runtime.dependencies._installed_requirements', side_effect=[(), ('textual>=8,<9',), (), ()]), patch('api_agent.runtime.dependencies.subprocess.run') as install, patch('api_agent.runtime.dependencies.subprocess.call', return_value=0):
        ensure_environment(context, [])
        ensure_environment(context, [])
        assert install.call_count == 1
        ensure_environment(context, [])
        assert install.call_count == 1


def test_target_interpreter_probe_checks_constraints_and_markers():
    import sys
    assert _installed_requirements(Path(sys.executable), ('packaging>=999', 'absent-package-for-test>=1',
                                                          'ignored-package-for-test; python_version < "2"')) == (
        'packaging>=999', 'absent-package-for-test>=1')


def test_direct_url_requires_explicit_verification():
    import sys
    import pytest
    with pytest.raises(ValueError, match='Direct URL requirement'):
        _installed_requirements(Path(sys.executable), ('example @ https://example.invalid/archive.whl',))


def test_failed_probe_repairs_then_verifies_before_marker(tmp_path):
    base = tmp_path / 'base'; base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["packaging>=24"]\n')
    environment = base / '.venv'; (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    context = SimpleNamespace(base_root=base, pack_root=base, external_pack=False)
    with patch('api_agent.runtime.dependencies.subprocess.run', side_effect=__import__('subprocess').CalledProcessError(1, 'probe')), \
         patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), \
         patch('api_agent.runtime.dependencies.subprocess.call', return_value=0):
        from api_agent.runtime.dependencies import _installed_requirements
        with pytest.raises(OSError, match='Cannot verify dependencies'):
            _installed_requirements(environment / 'bin/python', ('packaging>=24',))
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), \
         patch('api_agent.runtime.dependencies._installed_requirements', return_value=('packaging>=24',)), \
         patch('api_agent.runtime.dependencies.subprocess.run'):
        with pytest.raises(OSError, match='remain unavailable'):
            ensure_environment(context, [])
        assert not (environment / '.api-agent-dependencies').exists()


def test_probe_checks_ordinary_and_requested_extra_children(tmp_path):
    import sys
    dist = tmp_path / 'parent_test-1.0.dist-info'
    dist.mkdir()
    (dist / 'METADATA').write_text('Metadata-Version: 2.3\nName: parent-test\nVersion: 1.0\n'
                                   'Requires-Dist: missing-child-for-test>=1\n'
                                   'Provides-Extra: browser\n'
                                   'Requires-Dist: missing-extra-for-test>=2; extra == "browser"\n')
    with patch.dict('os.environ', {'PYTHONPATH': str(tmp_path)}):
        missing = _installed_requirements(Path(sys.executable), ('parent-test[browser]>=1',))
    assert set(missing) == {'missing-child-for-test>=1', 'missing-extra-for-test>=2; extra == "browser"'}


def test_unknown_optional_group_fails_before_any_installation(tmp_path):
    import pytest
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    with patch('api_agent.runtime.dependencies.subprocess.run') as run:
        with pytest.raises(ValueError, match='Unknown optional dependency group.*browser'):
            ensure_environment(context, [], groups=('browser',))
    run.assert_not_called()


def test_direct_url_installation_fails_before_creating_environment(tmp_path):
    import pytest
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\ndependencies=["example @ https://example.invalid/package.whl"]\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    with patch('api_agent.runtime.dependencies.subprocess.run') as run:
        with pytest.raises(ValueError, match='Direct URL requirement'):
            ensure_environment(context, [])
    run.assert_not_called()
    assert not (tmp_path / '.venv').exists()


def test_ready_environment_group_switch_does_not_install_or_write_marker(tmp_path):
    (tmp_path/'pyproject.toml').write_text('[project]\ndependencies=["base>=1"]\n[project.optional-dependencies]\nbrowser=["browser>=1"]\n')
    environment=tmp_path/'.venv'
    (environment/'bin').mkdir(parents=True)
    (environment/'bin/python').touch()
    context=SimpleNamespace(base_root=tmp_path,pack_root=tmp_path,external_pack=False)
    with patch('api_agent.runtime.dependencies._installed_requirements',return_value=()) as probe, \
         patch('api_agent.runtime.dependencies.sys.prefix',str(environment)), \
         patch('api_agent.runtime.dependencies.subprocess.run') as install, \
         patch.object(Path,'write_text',side_effect=PermissionError('read-only environment')) as write:
        assert ensure_environment(context,[]) is None
        assert ensure_environment(context,[],groups=('browser',)) is None
        assert probe.call_args_list[0].args[1] == ('base>=1',)
        assert probe.call_args_list[1].args[1] == ('base>=1','browser>=1')
        install.assert_not_called(); write.assert_not_called()


def test_empty_requirements_still_create_missing_environment(tmp_path):
    context=SimpleNamespace(base_root=tmp_path,pack_root=tmp_path,external_pack=False)
    environment=tmp_path/'.venv'
    def create(*args,**kwargs):
        (environment/'bin').mkdir(parents=True)
        (environment/'bin/python').touch()
    with patch('api_agent.runtime.dependencies.shutil.which',return_value=None), \
         patch('api_agent.runtime.dependencies.subprocess.run',side_effect=create) as run, \
         patch('api_agent.runtime.dependencies.sys.prefix',str(environment)):
        assert ensure_environment(context,[]) is None
        assert run.call_count == 1
        assert (environment/'.api-agent-dependencies').is_file()


def test_active_environment_recognizes_physical_alias_and_missing_path(tmp_path, monkeypatch):
    from api_agent.runtime.dependencies import active_environment
    target = tmp_path / 'venv'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.mkdir()
    monkeypatch.setattr('api_agent.runtime.dependencies.sys.prefix', str(alias))
    with patch.object(Path, 'samefile', return_value=True) as samefile:
        assert active_environment(target)
        samefile.assert_called_once_with(target)
    assert not active_environment(tmp_path / 'absent')
    monkeypatch.setattr('api_agent.runtime.dependencies.sys.prefix', str(target))
    assert active_environment(target)


def test_missing_host_parser_hands_off_to_existing_environment_before_reading_manifests(tmp_path, monkeypatch):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(dependencies, '_requirement_types', lambda: (_ for _ in ()).throw(OSError('missing parser')))
    with patch.object(dependencies, 'requirements', side_effect=AssertionError('host cannot parse yet')), \
         patch.object(dependencies.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as probe, \
         patch.object(dependencies.subprocess, 'call', return_value=7) as launch:
        assert dependencies.ensure_environment(context, ['--tui-mode=unit', '--collect-only']) == 7
    assert probe.call_count == 1
    assert probe.call_args.args[0][0] == str(python)
    assert launch.call_args.args[0][-2:] == ['--tui-mode=unit', '--collect-only']


@pytest.mark.parametrize('uv', ['uv', None])
def test_fresh_validator_bootstrap_uses_seeded_environment_not_manifest_requirements(tmp_path, monkeypatch, uv):
    from api_agent.runtime.dependencies import _prepare_requirement_parser
    environment = tmp_path / '.venv'
    python = environment / 'bin/python'
    monkeypatch.setattr('api_agent.runtime.dependencies.shutil.which', lambda _: uv)
    with patch('api_agent.runtime.dependencies.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
        _prepare_requirement_parser(environment, python)
    create, probe = [call.args[0] for call in run.call_args_list]
    assert create[-1] == str(environment)
    assert ('--seed' in create) is bool(uv)
    assert create[:2] == ['uv', 'venv'] if uv else create[1:3] == ['-m', 'venv']
    assert probe[0] == str(python)
    assert not any('install' in command for command in (create, probe))


@pytest.mark.parametrize('uv', ['uv', None])
def test_missing_target_parser_installs_only_fixed_validator_then_verifies(tmp_path, monkeypatch, uv):
    from api_agent.runtime.dependencies import _prepare_requirement_parser
    python = tmp_path / 'bin/python'
    python.parent.mkdir()
    python.touch()
    monkeypatch.setattr('api_agent.runtime.dependencies.shutil.which', lambda _: uv)
    replies = [SimpleNamespace(returncode=3), SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)]
    if not uv:
        replies.insert(1, SimpleNamespace(returncode=0))
    with patch('api_agent.runtime.dependencies.subprocess.run', side_effect=replies) as run:
        _prepare_requirement_parser(tmp_path, python)
    commands = [call.args[0] for call in run.call_args_list]
    installer = commands[-2]
    assert installer[-1] == 'packaging>=24,<27'
    assert installer[:3] == ['uv', 'pip', 'install'] if uv else installer[:4] == [str(python), '-m', 'pip', 'install']
    assert commands[0] == commands[-1]
    if not uv:
        assert commands[1] == [str(python), '-m', 'ensurepip']


def test_validator_probe_failure_never_becomes_installation_intent(tmp_path):
    from api_agent.runtime.dependencies import _prepare_requirement_parser
    python = tmp_path / 'python'
    python.touch()
    with patch('api_agent.runtime.dependencies.subprocess.run', side_effect=[
            SimpleNamespace(returncode=1), SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)]) as run:
        with pytest.raises(OSError, match='Cannot verify the dependency validator'):
            _prepare_requirement_parser(tmp_path, python)
    assert run.call_count == 1


def test_validator_still_missing_after_installation_prevents_handoff(tmp_path):
    from api_agent.runtime.dependencies import _prepare_requirement_parser
    python = tmp_path / 'python'
    python.touch()
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), \
         patch('api_agent.runtime.dependencies.subprocess.run', side_effect=[
             SimpleNamespace(returncode=3), SimpleNamespace(returncode=0), SimpleNamespace(returncode=3)]):
        with pytest.raises(OSError, match='remains unavailable'):
            _prepare_requirement_parser(tmp_path, python)


@pytest.mark.parametrize('failure', [OSError('unavailable'), __import__('subprocess').TimeoutExpired('probe', 20)])
def test_validator_probe_transport_error_is_a_launcher_error(tmp_path, failure):
    from api_agent.runtime.dependencies import _requirement_parser_status
    with patch('api_agent.runtime.dependencies.subprocess.run', side_effect=failure):
        with pytest.raises(OSError, match='Cannot verify the dependency validator'):
            _requirement_parser_status(tmp_path / 'python')


def test_cli_handoff_replaces_process_with_preserved_command_and_environment(tmp_path):
    from api_agent.runtime.dependencies import _relaunch
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    class Replaced(BaseException):
        pass
    with patch('api_agent.runtime.dependencies.os.execve', side_effect=Replaced) as replace, \
         patch('api_agent.runtime.dependencies.subprocess.call') as launch:
        with pytest.raises(Replaced):
            _relaunch(context, tmp_path / 'python', ['--debug'], replace_process=True)
    executable, command, environment = replace.call_args.args
    assert executable == str(tmp_path / 'python')
    assert command[0] == executable
    assert '_replace_process=True' in command[2]
    assert command[-1] == '--debug'
    assert environment == os.environ
    launch.assert_not_called()


def test_library_handoff_retains_waiting_child_status_and_cancellation_contract(tmp_path):
    from api_agent.runtime.dependencies import _relaunch
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    with patch('api_agent.runtime.dependencies.subprocess.call', side_effect=KeyboardInterrupt), \
         patch('api_agent.runtime.dependencies.os.execve') as replace:
        with pytest.raises(KeyboardInterrupt):
            _relaunch(context, tmp_path / 'python', [])
    replace.assert_not_called()


@pytest.mark.parametrize('missing_parser', [False, True])
def test_environment_forwards_process_replacement_only_when_requested(tmp_path, missing_parser):
    from api_agent.runtime import dependencies
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    with patch.object(dependencies, '_requirement_types', side_effect=OSError if missing_parser else None), \
         patch.object(dependencies, '_prepare_requirement_parser'), \
         patch.object(dependencies, '_relaunch', return_value=7) as relaunch:
        assert dependencies.ensure_environment(context, [], replace_process=True) == 7
    relaunch.assert_called_once_with(context, python, [], replace_process=True)


def test_process_replacement_failure_is_visible_without_fallback_launch(tmp_path):
    from api_agent.runtime.dependencies import _relaunch
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)
    with patch('api_agent.runtime.dependencies.os.execve', side_effect=OSError('synthetic exec failure')), \
         patch('api_agent.runtime.dependencies.subprocess.call') as launch:
        with pytest.raises(OSError, match='synthetic exec failure'):
            _relaunch(context, tmp_path / 'python', [], replace_process=True)
    launch.assert_not_called()
