"""Local tool setup tests; no package download or real venv is performed."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
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
        assert install.call_count == 1
        assert '_worktrees_verified=True' in launch.call_args.args[0][2]
        assert launch.call_args.args[0][-1] == '--tui-mode=config-cleanup'
        assert launch.call_args.args[0][-2] == str(base)
        assert '--_launcher-root' in launch.call_args.args[0]
        assert ensure_environment(context, ['--tui-mode=config-cleanup']) == 3
        assert install.call_count == 1
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
        assert install.call_count == 2
        ensure_environment(context, [])
        assert install.call_count == 2


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
        assert _installed_requirements(environment / 'bin/python', ('packaging>=24',)) == ('packaging>=24',)
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), \
         patch('api_agent.runtime.dependencies._installed_requirements', return_value=('packaging>=24',)), \
         patch('api_agent.runtime.dependencies.subprocess.run'):
        import pytest
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
