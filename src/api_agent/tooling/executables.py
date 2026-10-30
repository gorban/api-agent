"""Capture selected executable data; acquire toolchains only at runtime start."""
import json
from functools import partial
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from types import MappingProxyType

from api_agent.providers.progress import report_timeout_sync
from .models import ToolSelectionError

MAX_MANIFESTS = 64
MAX_TOOLS = 64
BACKEND = re.compile(r'(?:core|aqua|github|cargo|dotnet|npm):[A-Za-z0-9][A-Za-z0-9_./-]{0,160}\Z')
VERSION = re.compile(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:[+._-][A-Za-z0-9._+-]+)?\Z')


def validate_tools(tools):
    if (not isinstance(tools, (dict, MappingProxyType)) or len(tools) > MAX_TOOLS
            or any(type(name) is not str or not BACKEND.fullmatch(name)
                   or type(version) is not str or not VERSION.fullmatch(version)
                   for name, version in tools.items())):
        raise ToolSelectionError('Executable tools require qualified mise identities and exact versions')


def capture_executables(capture, implementation, target):
    """Read an explicitly linked closure using the ordinary resource capture."""
    tools, assets, visiting = {}, {}, set()

    def linked(source, value):
        if (type(value) is not str or not value or Path(value).is_absolute()
                or ':' in value or value.startswith('<resource_pack>/')):
            raise ToolSelectionError('Executable requirements need an owner-relative file path')
        path = capture.path(source, value)
        if capture.owner(path) != capture.owner(source):
            raise ToolSelectionError('Executable requirement link escapes its owner')
        return path

    def visit(path):
        if path in visiting:
            raise ToolSelectionError('Cyclic executable requirements')
        if path in assets:
            return
        if len(assets) + len(visiting) >= MAX_MANIFESTS:
            raise ToolSelectionError('Executable requirements exceed manifest limit')
        visiting.add(path)
        data = capture.toml(path)
        if (set(data) - {'version', 'tools', 'requires'} or type(data.get('version')) is not int
                or data['version'] != 1 or type(data.get('requires', [])) is not list
                or len(data.get('requires', [])) > MAX_MANIFESTS):
            raise ToolSelectionError('Invalid version 1 executable requirements')
        selected = data.get('tools', {})
        validate_tools(selected)
        for name, version in selected.items():
            if name in tools and tools[name] != version:
                raise ToolSelectionError('Conflicting executable versions for ' + name)
            tools[name] = version
        if len(tools) > MAX_TOOLS:
            raise ToolSelectionError('Executable requirements exceed tool limit')
        for child in data.get('requires', []):
            visit(linked(path, child))
        visiting.remove(path)
        assets[path] = capture.bodies[path]

    if target is not None:
        visit(linked(implementation, target))
    return tools, assets


def _command(mise, arguments, directory, environment, budget, cancellation):
    if cancellation.is_set():
        raise InterruptedError('Toolchain preparation cancelled')
    if budget.remaining is not None and budget.remaining <= 0:
        raise TimeoutError('Toolchain preparation timed out')
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen([mise, '--cd', str(directory), '--no-env', '--no-hooks', *arguments],
                cwd=directory, env=environment, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            raise OSError('Toolchain preparation could not start; check mise') from None
        try:
            while True:
                if cancellation.is_set():
                    raise InterruptedError('Toolchain preparation cancelled')
                if budget.remaining is not None and budget.remaining <= 0:
                    raise TimeoutError('Toolchain preparation timed out')
                try:
                    process.wait(timeout=min(.1, budget.remaining or .1))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise OSError('Toolchain preparation failed; check declared versions and host prerequisites')
            output.seek(0)
            body = output.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise OSError('Toolchain environment exceeds output limit')
            return body
        finally:
            from api_agent.testing.processes import finalize
            finalize(process)


def prepare_environment(tools, *, timeout=180, cancellation=None):
    """Return tool-specific overrides, never changing the parent's environment."""
    validate_tools(tools)
    if not tools:
        return MappingProxyType({})
    mise = shutil.which('mise')
    if mise is None:
        raise OSError('Selected tool requires mise on PATH; follow README setup and use launch.sh')
    cancellation = cancellation if cancellation is not None else threading.Event()
    # Source launchers already supply isolated data/cache directories. Direct
    # consumers use their user cache, never a contributed source directory.
    storage = Path(os.environ.get('MISE_DATA_DIR', Path.home() / '.cache' / 'api-agent' / 'mise')).absolute()
    with report_timeout_sync('Selected toolchain preparation', timeout) as budget, tempfile.TemporaryDirectory(prefix='api-agent-tools-') as temporary:
        directory = Path(temporary)
        environment = {key: value for key, value in os.environ.items() if not key.startswith('MISE_')}
        environment.update(MISE_DATA_DIR=str(storage), MISE_CACHE_DIR=str(storage / 'cache'),
            MISE_STATE_DIR=str(storage / 'state'), MISE_CONFIG_DIR=str(directory / 'config'),
            MISE_GLOBAL_CONFIG_FILE=str(directory / 'global.toml'),
            MISE_SYSTEM_CONFIG_DIR=str(directory / 'system'),
            MISE_SYSTEM_CONFIG_FILE=str(directory / 'system.toml'),
            MISE_CEILING_PATHS=str(directory.parent), MISE_OVERRIDE_CONFIG_FILENAMES='mise.toml',
            MISE_OVERRIDE_TOOL_VERSIONS_FILENAMES='disabled-tool-versions',
            MISE_ENV='', MISE_NO_ENV='1', MISE_NO_HOOKS='1')
        # Render only validated literals. No component config, templates, tasks,
        # aliases, installation options or hooks are passed through to mise.
        config = '[settings]\ndotnet.isolated = true\n'
        config += 'rust.cargo_home = ' + json.dumps(str(storage / 'cargo')) + '\n'
        config += 'rust.rustup_home = ' + json.dumps(str(storage / 'rustup')) + '\n[tools]\n'
        config += ''.join(json.dumps(name) + ' = ' + json.dumps(version) + '\n'
                          for name, version in sorted(tools.items()))
        (directory / 'mise.toml').write_text(config, encoding='utf-8')
        _command(mise, ['install'], directory, environment, budget, cancellation)
        body = _command(mise, ['env', '--json'], directory, environment, budget, cancellation)
        try:
            values = json.loads(body)
            if (type(values) is not dict or not values.get('PATH')
                    or any(type(key) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
                           or type(value) is not str or '\0' in value for key, value in values.items())):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise OSError('Mise returned an invalid toolchain environment') from None
        if cancellation.is_set():
            raise InterruptedError('Toolchain preparation cancelled')
        return MappingProxyType(values)


async def prepare_environments(plan):
    """Prepare each distinct selected closure once; cancellation drains installers."""
    from api_agent.runtime.tasks import offload
    prepared, environments = {}, {}
    for tool in plan.tools:
        if not tool.executables:
            continue
        key = tuple(sorted(tool.executables.items()))
        if key not in prepared:
            cancellation = threading.Event()
            prepared[key] = await offload(partial(prepare_environment, dict(key),
                cancellation=cancellation, timeout=180), cancellation=cancellation)
        environments[tool.id] = prepared[key]
    return MappingProxyType(environments)
