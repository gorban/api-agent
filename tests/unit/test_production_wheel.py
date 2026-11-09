"""The production wheel is a parser-free, noninteractive runtime artifact."""
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_production_build_target_excludes_development_import_closure(tmp_path):
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    wheel = build(ROOT, tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = next(name for name in names if name.endswith('.dist-info/METADATA'))
        requires = archive.read(metadata).decode()

    forbidden_parts = (
        '.md.html', '.svg', '/viewer/', '/ui/', '/tui/', '/tests/', '/test_',
        'workflows/definition.py', 'workflows/document_format.py', 'workflows/layout.py',
        'runtime/trace_viewer.py', 'runtime/tracing_viewer_config.py',
        'runtime/viewer_host.py', 'runtime/viewer_modal.py',
        'workflows/viewer_projection.py', 'workflows/viewer_repository.py',
    )
    assert not [name for name in names if any(part in '/' + name for part in forbidden_parts)]
    assert 'api_agent/runtime/production.py' in names
    assert 'api_agent/cfg/config.toml' in names
    assert 'api_agent/telemetry/trace_adapters/otel/cfg/config.toml' in names
    assert 'api_agent/demo/simple/functions.py' in names
    assert 'Requires-Dist: textual' not in requires
    assert 'Requires-Dist: playwright' not in requires
    assert 'Requires-Dist: pytest' not in requires


def test_production_import_closure_never_imports_terminal_or_viewer_modules(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    closure = module['production_import_closure'](ROOT)
    forbidden_modules = {
        'api_agent.runtime.trace_viewer',
        'api_agent.runtime.tracing_viewer_config',
        'api_agent.runtime.viewer_host',
        'api_agent.runtime.viewer_modal',
        'api_agent.workflows.viewer_projection',
        'api_agent.workflows.viewer_repository',
    }
    assert not closure & forbidden_modules

    build = module['build']
    wheel = build(ROOT, tmp_path)
    site = tmp_path / 'site'
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    result = subprocess.run((
        sys.executable, '-I', '-c',
        "import asyncio,sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "import api_agent.runtime.production as production\n"
        "class Runtime:\n"
        " async def astart(self): pass\n"
        " async def aclose(self): pass\n"
        "async def request(scope,receive,send): pass\n"
        "production.load_artifact=lambda _: object()\n"
        "production.selected_application=lambda *a,**k: SimpleNamespace(requires_api=False)\n"
        "production.compose_chat_runtime=lambda *a,**k: SimpleNamespace(runtime=Runtime())\n"
        "production.create_application=lambda *a,**k: request\n"
        "settings=production.ProductionSettings(Path('/dev/null'),'pack',Path('/'),"
        "'127.0.0.1',8080,'https://localhost:9443','http://localhost:3000','n'*32,{},1)\n"
        "app=production.ProductionApplication(settings)\n"
        "async def exercise():\n"
        " await app._start()\n"
        " await app({'type':'http'},lambda:None,lambda _:None)\n"
        "asyncio.run(exercise())\n"
        "assert not [name for name in sys.modules if name.startswith(('textual','api_agent.tui',"
        "'api_agent.ui','api_agent.workflows.definition','api_agent.workflows.viewer',"
        "'api_agent.runtime.trace_viewer','api_agent.runtime.tracing_viewer_config',"
        "'api_agent.runtime.viewer_host','api_agent.runtime.viewer_modal'))]\n",
        str(site),
    ), capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_production_wheel_main_uses_the_repository_output(monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    built = []
    monkeypatch.setitem(module['main'].__globals__, 'build',
                        lambda root, output: built.append((root, output)) or output / 'wheel.whl')

    assert module['main']() == 0
    assert built == [(ROOT, ROOT / 'dist-production')]


def test_production_wheel_build_rejects_retained_or_missing_artifacts(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    scope = module['build'].__globals__
    monkeypatch.setitem(scope, 'production_import_closure',
                        lambda _: frozenset({'api_agent.runtime.viewer_host'}))

    with pytest.raises(RuntimeError, match='requires excluded modules'):
        module['build'](ROOT, tmp_path)

    monkeypatch.setitem(scope, 'production_import_closure', module['production_import_closure'])
    monkeypatch.setattr(module['subprocess'], 'run', lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match='exactly one wheel'):
        module['build'](ROOT, tmp_path)


def test_production_import_helper_supports_package_initializers(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    package = tmp_path / 'api_agent' / 'runtime'
    package.mkdir(parents=True)
    initializer = package / '__init__.py'
    initializer.write_text('')

    assert module['_module_source'](tmp_path, 'api_agent.runtime') == initializer


def test_production_import_closure_ignores_missing_local_imports(monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    scope = module['production_import_closure'].__globals__
    monkeypatch.setitem(scope, '_module_source', lambda *_args: None)

    assert module['production_import_closure'](ROOT) == frozenset()


def test_production_wheel_script_exits_with_main_result(monkeypatch, tmp_path):
    script = tmp_path / 'scripts/build_production_wheel.py'
    script.parent.mkdir()
    script.write_text((ROOT / 'scripts/build_production_wheel.py').read_text())
    for relative in ('src/api_agent/runtime/production.py', 'cfg/config.toml',
                     'demo/simple/functions.py', 'workflows/contracts/response.json', 'LICENSE'):
        fixture = tmp_path / relative
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text('pass\n')

    def build_wheel(command, *, cwd, check, capture_output, text):
        assert cwd == tmp_path
        output = Path(command[command.index('--out-dir') + 1])
        assert output == tmp_path / 'dist-production'
        output.joinpath('api_agent_runtime-0.1.0-py3-none-any.whl').write_bytes(b'')

    monkeypatch.setattr(subprocess, 'run', build_wheel)

    with pytest.raises(SystemExit) as exit_code:
        runpy.run_path(str(script), run_name='__main__')

    assert exit_code.value.code == 0
