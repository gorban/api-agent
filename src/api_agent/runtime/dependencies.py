"""Prepare local runtime tools without installing either source checkout."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib


def requirements(context, *, collect=False, groups=()):
    dependencies = []
    found_groups = set()
    for root in dict.fromkeys((context.base_root, context.pack_root)):
        manifest = root / 'pyproject.toml'
        if not manifest.is_file():
            continue
        with manifest.open('rb') as handle:
            project = tomllib.load(handle).get('project', {})
        selected = list(project.get('dependencies', []))
        optional = project.get('optional-dependencies', {})
        for group in groups:
            if group in optional:
                found_groups.add(group)
                selected.extend(optional[group])
        for requirement in selected:
            # Source loading supplies the base itself; install only external tools.
            name = re.split(r'[<>=!~;\[ ]', requirement, maxsplit=1)[0].lower().replace('_', '-')
            if name != 'api-agent':
                dependencies.append(requirement)
    if missing_groups := set(groups) - found_groups:
        raise ValueError(f'Unknown optional dependency group(s): {", ".join(sorted(missing_groups))}')
    if collect:
        dependencies.extend(('pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4'))
    return tuple(dict.fromkeys(dependencies))


def _installed_requirements(python, wanted):
    """Probe the target interpreter; a fingerprint alone cannot prove installation."""
    for requirement in wanted:
        if re.search(r'\s@\s', requirement):
            raise ValueError(f'Direct URL requirement needs explicit verification: {requirement}')
    script = '''
import importlib.metadata as metadata
import json, sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.markers import default_environment
result = []
seen = set()
def check(raw, extra=''):
    req = Requirement(raw)
    if req.url:
        raise ValueError(f'Direct URL requirement needs explicit verification: {raw}')
    environment = default_environment()
    environment['extra'] = extra
    if req.marker and not req.marker.evaluate(environment):
        return
    key = (canonicalize_name(req.name), str(req.specifier), tuple(sorted(req.extras)))
    if key in seen:
        return
    seen.add(key)
    try:
        distribution = metadata.distribution(canonicalize_name(req.name))
    except metadata.PackageNotFoundError:
        result.append(raw)
        return
    if distribution.version not in req.specifier:
        result.append(raw)
    for child in distribution.requires or ():
        check(child)
    for selected_extra in req.extras:
        for child in distribution.requires or ():
            check(child, selected_extra)
for raw in json.loads(sys.argv[1]):
    check(raw)
print(json.dumps(result))
'''
    try:
        probe = subprocess.run([str(python), '-c', script, json.dumps(wanted)],
                               capture_output=True, text=True, timeout=20, check=True)
        return tuple(json.loads(probe.stdout))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError):
        return wanted


def active_environment(environment):
    """Recognize the active venv through filesystem aliases as well as paths."""
    active = Path(sys.prefix)
    if active.resolve() == environment.resolve():
        return True
    try:
        return active.samefile(environment)
    except OSError:
        return False


def ensure_environment(context, argv, *, collect=False, groups=()):
    """Return a relaunched command's status, or None in the ready local venv."""
    root = context.pack_root if context.external_pack else context.base_root
    environment = root / '.venv'
    python = environment / 'bin/python'
    wanted = requirements(context, collect=collect, groups=groups)
    for requirement in wanted:
        if re.search(r'\s@\s', requirement):
            raise ValueError(f'Direct URL requirement needs explicit verification: {requirement}')
    fingerprint = hashlib.sha256(json.dumps(wanted).encode()).hexdigest()
    marker = environment / '.api-agent-dependencies'
    missing = _installed_requirements(python, wanted) if python.is_file() and wanted else wanted
    ready = python.is_file() and not missing
    if not ready:
        print(f'Preparing development tools in {environment}...', file=sys.stderr, flush=True)
        uv = shutil.which('uv')
        if not python.is_file():
            command = [uv, 'venv', '--python', sys.executable, str(environment)] if uv else [sys.executable, '-m', 'venv', str(environment)]
            subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
        if wanted:
            command = ([uv, 'pip', 'install', '--python', str(python), *wanted] if uv else
                       [str(python), '-m', 'pip', 'install', *wanted])
            subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
            if unresolved := _installed_requirements(python, wanted):
                raise OSError(f'Installed dependencies remain unavailable or incompatible: {", ".join(unresolved)}')
        marker.write_text(fingerprint + '\n')
    if active_environment(environment):
        return None
    pack_arguments = [] if getattr(context, 'show_pack_bootstrap', False) else [
        '--resource-pack-dir', str(context.pack_root)]
    # The parent already completed the gate; this is an internal interpreter handoff,
    # not another user launch. No command-line or environment approval flag exists.
    handoff = (
        "import sys; from pathlib import Path; "
        "base = Path(sys.argv[1]).resolve().parent; "
        "sys.path.insert(0, str(base / 'src')); "
        "from api_agent.runtime.bootstrap import main; "
        "raise SystemExit(main(sys.argv[2:], base_root=base, _worktrees_verified=True))"
    )
    return subprocess.call([str(python), '-c', handoff, str(context.base_root / 'launch.py'),
                            *pack_arguments,
                            '--_launcher-root', str(getattr(context, 'launcher_root', None) or context.base_root), *argv], env=os.environ.copy())
