"""Local tool setup tests; no package download or real venv is performed."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from api_agent.runtime.dependencies import ensure_environment, requirements


def test_requirements_merge_without_source_install(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["textual>=8,<9"]\n')
    (pack / 'pyproject.toml').write_text('[project]\ndependencies=["api-agent==0.1.0", "other>=1"]\n')
    context = SimpleNamespace(base_root=base, pack_root=pack)
    assert requirements(context, collect=True) == ('textual>=8,<9', 'other>=1', 'pytest>=8,<10')


def test_prepare_once_reexec_and_preserve_arguments(tmp_path):
    base = tmp_path / 'base'; base.mkdir()
    (base / 'pyproject.toml').write_text('[project]\ndependencies=["textual>=8,<9"]\n')
    environment = base / '.venv'; (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    context = SimpleNamespace(base_root=base, pack_root=base, external_pack=False)
    with patch('api_agent.runtime.dependencies.shutil.which', return_value='uv'), patch('api_agent.runtime.dependencies.subprocess.run') as install, patch('api_agent.runtime.dependencies.subprocess.call', return_value=3) as launch:
        assert ensure_environment(context, ['--tui-mode=config-cleanup']) == 3
        assert install.call_count == 1
        assert launch.call_args.args[0][-1] == '--tui-mode=config-cleanup'
        assert launch.call_args.args[0][-2] == str(base)
        assert '--_launcher-root' in launch.call_args.args[0]
        assert ensure_environment(context, ['--tui-mode=config-cleanup']) == 3
        assert install.call_count == 1
    with patch('api_agent.runtime.dependencies.sys.prefix', str(environment)), patch('api_agent.runtime.dependencies.subprocess.call') as launch:
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
