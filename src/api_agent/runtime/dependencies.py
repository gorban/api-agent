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


def requirements(context, *, collect=False):
    dependencies = []
    for root in dict.fromkeys((context.base_root, context.pack_root)):
        manifest = root / 'pyproject.toml'
        if not manifest.is_file():
            continue
        with manifest.open('rb') as handle:
            project = tomllib.load(handle).get('project', {})
        for requirement in project.get('dependencies', []):
            # Source loading supplies the base itself; install only external tools.
            name = re.split(r'[<>=!~;\[ ]', requirement, maxsplit=1)[0].lower().replace('_', '-')
            if name != 'api-agent':
                dependencies.append(requirement)
    if collect:
        dependencies.append('pytest>=8,<10')
    return tuple(dict.fromkeys(dependencies))


def ensure_environment(context, argv, *, collect=False):
    """Return a relaunched command's status, or None in the ready local venv."""
    root = context.pack_root if context.external_pack else context.base_root
    environment = root / '.venv'
    python = environment / 'bin/python'
    wanted = requirements(context, collect=collect)
    fingerprint = hashlib.sha256(json.dumps(wanted).encode()).hexdigest()
    marker = environment / '.api-agent-dependencies'
    ready = python.is_file() and marker.is_file() and marker.read_text().strip() == fingerprint
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
        marker.write_text(fingerprint + '\n')
    if Path(sys.prefix).resolve() == environment.resolve():
        return None
    return subprocess.call([str(python), str(context.base_root / 'launch.py'),
                            '--resource-pack-dir', str(context.pack_root), *argv], env=os.environ.copy())
