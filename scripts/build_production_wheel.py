"""Build the parser-free production wheel from a clean temporary source tree."""
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


_EXCLUDED_DIRECTORIES = frozenset({
    '__pycache__', 'testing', 'tests', 'tui', 'ui',
})
# Provider packages the image selects by file location at runtime, so no
# import from the entrypoint reaches them.
_PROVIDER_PACKAGES = ('llm_providers/bedrock', 'api_providers/graphql_mcp')
_EXCLUDED_MODULE_FILES = frozenset({
    # Source-display and interactive-flow modules.  The import-closure tests
    # cover the entrypoint's import-time, startup, and request paths.
    'runtime/local_browser.py',
    'runtime/trace_viewer.py',
    'runtime/tracing_viewer_config.py',
    'runtime/view_flow.py',
    'runtime/viewer_host.py',
    'runtime/viewer_modal.py',
    'workflows/definition.py',
    'workflows/native.py',
    'workflows/document_format.py',
    'workflows/layout.py',
    'workflows/viewer_build.py',
    'workflows/viewer_config.py',
    'workflows/viewer_projection.py',
    'workflows/viewer_render.py',
    'workflows/viewer_repository.py',
})

# Project dependencies imported only by the excluded declaration parser.
_PARSER_DISTRIBUTIONS = frozenset({'markdown-it-py'})
_DISTRIBUTION_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')

_PYPROJECT = '''\
[build-system]
requires = ["hatchling>=1.26,<2"]
build-backend = "hatchling.build"

[project]
name = "api-agent-runtime"
version = "0.1.0"
description = "Compiled-workflow API agent runtime"
requires-python = ">=3.13"
dependencies = {dependencies}

[tool.hatch.build.targets.wheel]
packages = ["src/api_agent"]

[tool.hatch.build.targets.wheel.force-include]
"src/api_agent/cfg/config.toml" = "api_agent/cfg/config.toml"
"src/api_agent/telemetry/trace_adapters/otel/cfg/config.toml" = "api_agent/telemetry/trace_adapters/otel/cfg/config.toml"
'''


def _normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def runtime_dependencies(root):
    """The project's declared requirements, verbatim, without the parser-only distributions."""
    declared = tomllib.loads((Path(root) / 'pyproject.toml').read_text())['project']['dependencies']
    named = [(_normalized(_DISTRIBUTION_NAME.match(item).group()), item) for item in declared]
    stale = sorted(_PARSER_DISTRIBUTIONS - {name for name, _ in named})
    if stale:
        raise RuntimeError(f'parser-only distributions are not project dependencies: {stale}')
    return [item for name, item in named if name not in _PARSER_DISTRIBUTIONS]


def _module_source(package_root, module):
    candidate = package_root.joinpath(*module.split('.'))
    source = candidate.with_suffix('.py')
    if source.is_file():
        return source
    initializer = candidate / '__init__.py'
    return initializer if initializer.is_file() else None


def _local_imports(module, source):
    package = module.rsplit('.', 1)[0]
    imports = set()
    for node in ast.parse(source.read_text(), filename=str(source)).body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name == 'api_agent'
                           or alias.name.startswith('api_agent.'))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split('.')
                base = '.'.join(parts[:len(parts) - node.level + 1])
            else:
                base = ''
            target = '.'.join(part for part in (base, node.module or '') if part)
            if target == 'api_agent' or target.startswith('api_agent.'):
                imports.add(target)
                imports.update(
                    target + '.' + alias.name
                    for alias in node.names
                    if alias.name != '*')
    return imports


def _closure(root, pending):
    package_root = Path(root).resolve() / 'src' / 'api_agent'
    pending, visited = list(pending), set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        source = _module_source(package_root.parent, module)
        if source is None:
            continue
        visited.add(module)
        for imported in _local_imports(module, source):
            if imported not in visited and _module_source(package_root.parent, imported):
                pending.append(imported)
    return frozenset(visited)


def production_import_closure(root):
    """Return the local import-time closure of the production entrypoint.

    Deferred startup and request imports are exercised independently by the
    production-wheel test, which snapshots the actual interpreter module set.
    """
    return _closure(root, ['api_agent.runtime.production'])


def provider_import_closure(root):
    """Return the local import-time closure of every module in the shipped provider packages."""
    package = Path(root).resolve() / 'src' / 'api_agent'
    return _closure(root, (
        '.'.join(('api_agent', *source.relative_to(package).with_suffix('').parts))
        for provider in _PROVIDER_PACKAGES
        for source in sorted((package / provider).glob('*.py'))))


def _ignore(directory, names):
    relative = Path(directory)
    excluded = set()
    for name in names:
        if name in _EXCLUDED_DIRECTORIES or name.endswith(('.pyc', '.md.html', '.svg')):
            excluded.add(name)
        elif (relative.name + '/' + name) in _EXCLUDED_MODULE_FILES:
            excluded.add(name)
    return excluded


def build(root, output):
    """Return one deterministic runtime wheel in ``output`` without dev assets."""
    root, output = Path(root).resolve(), Path(output).resolve()
    excluded_files = {'api_agent.' + module[:-3].replace('/', '.') for module in _EXCLUDED_MODULE_FILES}
    retained = {module for module in production_import_closure(root) | provider_import_closure(root)
                if module in excluded_files or _EXCLUDED_DIRECTORIES & set(module.split('.'))}
    if retained:
        raise RuntimeError(f'production import closure requires excluded modules: {sorted(retained)}')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-image-wheel-') as temporary:
        source = Path(temporary)
        package = source / 'src' / 'api_agent'
        shutil.copytree(root / 'src' / 'api_agent', package, ignore=_ignore)
        shutil.copytree(root / 'cfg', package / 'cfg')
        native = source / 'src' / 'api_agent' / 'demo' / 'simple'
        native.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / 'demo' / 'simple' / 'functions.py', native / 'functions.py')
        shutil.copytree(root / 'workflows' / 'contracts',
                        source / 'src' / 'api_agent' / 'workflows' / 'contracts')
        shutil.copy2(root / 'LICENSE', source / 'LICENSE')
        (source / 'pyproject.toml').write_text(
            _PYPROJECT.replace('{dependencies}', json.dumps(runtime_dependencies(root))))
        # Run from the checkout so a project-scoped uv (for example a mise shim) resolves.
        subprocess.run(('uv', 'build', '--wheel', '--out-dir', str(output), str(source)),
                       cwd=root, check=True, capture_output=True, text=True)
    wheels = sorted(output.glob('api_agent_runtime-*.whl'))
    if len(wheels) != 1:
        raise RuntimeError('production wheel build did not produce exactly one wheel')
    return wheels[0]


def main():
    """Build the production wheel in the repository-local output directory."""
    root = Path(__file__).resolve().parents[1]
    print(build(root, root / 'dist-production'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
