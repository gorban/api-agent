"""The production wheel is a parser-free, noninteractive runtime artifact."""
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile

import pytest
from test_workflow_stateful import fixture_graph


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
        'workflows/native.py',
        'runtime/trace_viewer.py', 'runtime/tracing_viewer_config.py',
        'runtime/viewer_host.py', 'runtime/viewer_modal.py',
        'workflows/viewer_projection.py', 'workflows/viewer_repository.py',
    )
    assert not [name for name in names if any(part in '/' + name for part in forbidden_parts)]
    assert 'api_agent/runtime/production.py' in names
    assert 'api_agent/workflows/configuration.py' in names
    assert 'api_agent/workflows/model.py' in names
    assert 'api_agent/cfg/config.toml' in names
    assert 'api_agent/telemetry/trace_adapters/otel/cfg/config.toml' in names
    assert 'api_agent/demo/simple/functions.py' in names
    assert 'Requires-Dist: textual' not in requires
    assert 'Requires-Dist: playwright' not in requires
    assert 'Requires-Dist: pytest' not in requires


def _requirements(lines):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    parsed = (Requirement(line) for line in lines)
    return {(canonicalize_name(item.name), frozenset(item.extras), item.specifier) for item in parsed}


def test_production_wheel_declares_the_project_dependencies_except_the_parser(tmp_path):
    import tomllib
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    declared = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['dependencies']

    wheel = build(ROOT, tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
        requires = [line.removeprefix('Requires-Dist: ') for line in archive.read(metadata).decode().splitlines()
                    if line.startswith('Requires-Dist: ')]

    assert _requirements(requires) == _requirements(
        item for item in declared if not item.startswith('markdown-it-py'))


def test_production_wheel_rejects_a_parser_exclusion_the_project_no_longer_declares(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))
    monkeypatch.setitem(module['build'].__globals__, '_PARSER_DISTRIBUTIONS', frozenset({'absent-parser'}))

    with pytest.raises(RuntimeError, match='absent-parser'):
        module['build'](ROOT, tmp_path)


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


@pytest.mark.parametrize('closure', ['production_import_closure', 'provider_import_closure'])
@pytest.mark.parametrize('module', ['api_agent.testing.diagnostics', 'api_agent.tui.app'])
def test_production_wheel_build_rejects_a_closure_reaching_an_excluded_package(monkeypatch, tmp_path, closure, module):
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    monkeypatch.setitem(build.__globals__, closure, lambda _: frozenset({module}))

    with pytest.raises(RuntimeError, match=f'requires excluded modules: .*{module}'):
        build(ROOT, tmp_path)


PROVIDER_PACKAGES = ('llm_providers/bedrock', 'api_providers/graphql_mcp')


def _source_files(package):
    return {path.relative_to(ROOT / 'src').as_posix()
            for path in (ROOT / 'src' / package).rglob('*')
            if path.is_file() and '__pycache__' not in path.parts
            and not path.name.endswith(('.md.html', '.svg', '.pyc'))
            and not {'tests', 'node_modules'} & set(path.relative_to(ROOT).parts)}


@pytest.mark.parametrize('package', ['api_agent/' + name for name in PROVIDER_PACKAGES])
def test_production_wheel_ships_every_file_of_the_bundled_provider_packages(tmp_path, package):
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']

    with zipfile.ZipFile(build(ROOT, tmp_path)) as archive:
        names = set(archive.namelist())

    assert _source_files(package) <= names


def test_production_wheel_ships_the_graphql_mcp_node_runtime_assets(tmp_path):
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    package = 'api_agent/api_providers/graphql_mcp/'

    with zipfile.ZipFile(build(ROOT, tmp_path)) as archive:
        names = set(archive.namelist())

    assert {package + 'package.json', package + 'package-lock.json', package + 'tool.toml',
            package + 'provider-dependencies.toml', package + 'licenses/mcp-graphql-MIT.txt',
            package + 'licenses/third-party/express/node_modules/content-type/LICENSE'} <= names


def test_installed_wheel_loads_both_bundled_providers_without_the_testing_package(tmp_path):
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    site = tmp_path / 'site'
    with zipfile.ZipFile(build(ROOT, tmp_path)) as archive:
        archive.extractall(site)

    result = subprocess.run((
        sys.executable, '-I', '-c',
        "import asyncio,importlib,pkgutil,sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "site=Path(sys.argv[1]).resolve()\n"
        "for name in ('api_agent.llm_providers.bedrock','api_agent.api_providers.graphql_mcp'):\n"
        " package=importlib.import_module(name)\n"
        " for info in pkgutil.walk_packages(package.__path__,name+'.'):\n"
        "  assert Path(importlib.import_module(info.name).__file__).resolve().is_relative_to(site),info.name\n"
        "from api_agent.llm_providers.bedrock import session_providers\n"
        "from api_agent.api_providers.graphql_mcp import assets\n"
        "acquirer=asyncio.run(session_providers.load_acquirer('',{},{}))\n"
        "assert type(acquirer).__name__=='SDKProfileAcquirer'\n"
        "directory=Path(assets.__file__).parent\n"
        "assert assets._sha256(directory/'package.json')==assets._PACKAGE_SHA256\n"
        "assert assets._sha256(directory/'package-lock.json')==assets._LOCK_SHA256\n"
        "assert not [n for n in sys.modules if n.startswith(('api_agent.testing','api_agent.tui','api_agent.ui'))]\n",
        str(site),
    ), capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


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
    (tmp_path / 'pyproject.toml').write_text('[project]\ndependencies = ["markdown-it-py==4.2.0"]\n')

    def build_wheel(command, *, cwd, check, capture_output, text):
        assert cwd == tmp_path
        output = Path(command[command.index('--out-dir') + 1])
        assert output == tmp_path / 'dist-production'
        output.joinpath('api_agent_runtime-0.1.0-py3-none-any.whl').write_bytes(b'')

    monkeypatch.setattr(subprocess, 'run', build_wheel)

    with pytest.raises(SystemExit) as exit_code:
        runpy.run_path(str(script), run_name='__main__')

    assert exit_code.value.code == 0


def test_installed_runtime_selects_and_executes_a_compiled_typed_application(tmp_path, fixture_graph):
    from artifact_acceptance_support import build_wheel
    from api_agent.workflows.artifact_compile import compile_definition
    from api_agent.workflows.definition import load_definition

    root, entry = fixture_graph
    source = root / 'application.py'
    source.write_text('''from types import SimpleNamespace
from api_agent.workflows.execution import Workflow
from langgraph.graph.message import add_messages

def create_application(definition, *, config):
    workflow = Workflow(definition, catalog=definition.load_catalog(config=config),
                        reducers={'messages': add_messages}, config=config)
    async def complete(request, *, llm, context):
        return await workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='one')
    return SimpleNamespace(definition=definition, complete=complete, timeouts={}, requires_api=False)
''')
    entry.write_text(entry.read_text().replace('## Flow', '## Application\n\n'
        '| Registry | Source |\n| --- | --- |\n| create_application | [source](application.py) |\n\n## Flow'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    bindings = build_wheel(tmp_path, 'application-bindings', {
        'native.py': (root / 'native.py').read_bytes(), 'application.py': source.read_bytes()})
    plan = tmp_path / 'plan.json'
    plan.write_bytes(compile_definition(definition, [bindings]))
    build = runpy.run_path(str(ROOT / 'scripts/build_production_wheel.py'))['build']
    runtime = build(ROOT, tmp_path / 'runtime')
    site = tmp_path / 'site'
    for wheel in (runtime, bindings):
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)

    completed = subprocess.run((sys.executable, '-I', '-c', '''import asyncio,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from api_agent.workflows.artifact import load_artifact
from api_agent.runtime.local_tools import selected_application
definition=load_artifact(Path(sys.argv[2]).read_bytes())
application=selected_application(definition)
result=asyncio.run(application.complete(None,llm=None,context=None))
assert result['visits']==1 and result['pending'] is True
assert application.definition is definition
assert 'api_agent.workflows.native' not in sys.modules
assert 'api_agent.workflows.definition' not in sys.modules
''', str(site), str(plan)), capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr


def _requirement_names(lines):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    return {canonicalize_name(Requirement(line).name) for line in lines}


def test_production_image_installs_the_extras_that_cover_each_bundled_provider_requirement():
    import re
    import tomllib
    dockerfile = (ROOT / 'Dockerfile').read_text()
    sync = next(line for line in dockerfile.splitlines() if 'uv sync' in line)
    extras = re.findall(r'--extra (\S+)', sync)
    declared = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['optional-dependencies']
    installed = _requirement_names(item for extra in extras for item in declared[extra])

    for package in PROVIDER_PACKAGES:
        manifest = tomllib.loads((ROOT / 'src/api_agent' / package / 'provider-dependencies.toml').read_text())
        assert _requirement_names(manifest['dependencies']) <= installed, package


def test_production_image_prepares_the_graphql_mcp_assets_where_the_provider_reads_them():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    prepared = '/build/owner/.api-agent/graphql-mcp'

    assert prepared in dockerfile
    assert 'COPY --from=compile /build/owner/.api-agent /opt/api-agent/plans/.api-agent' in dockerfile
    assert 'COPY --from=compile /opt/node/bin/node /opt/api-agent/bin/node' in dockerfile
    assert 'Path(owner_root) / ".api-agent" / "graphql-mcp"' in (
        ROOT / 'src/api_agent/api_providers/graphql_mcp/factory.py').read_text()
