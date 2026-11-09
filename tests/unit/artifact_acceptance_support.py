"""Harness plumbing for artifact acceptance: wheels, installs and isolated runs."""
from base64 import urlsafe_b64encode
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / 'tests' / 'unit'
QUALIFICATION = ROOT / 'tests' / 'fixtures' / 'artifact_qualification'
FORBIDDEN_MODULES = ('api_agent.workflows.definition', 'api_agent.workflows.document_format', 'markdown_it')

# Installed before any harness or candidate import, so it observes the whole
# production path. It records only events that touch admitted source files,
# plus every pickle class lookup.
_BOOT = r'''
import json, os, sys
spec = json.loads(sys.argv[1])
forbidden = frozenset(spec['forbidden_files'])
# Admission reads walk directories with dir_fd, so the audited path is a bare
# relative name; match those by basename.
forbidden_names = frozenset(os.path.basename(path) for path in forbidden)
audit = []
def _path(value):
    try:
        return os.path.realpath(os.fsdecode(value))
    except (TypeError, ValueError):
        return None
def _hook(name, args):
    if name == 'compile' and len(args) > 1 and _path(args[1]) in forbidden:
        audit.append(['compile', _path(args[1])])
    elif name == 'exec' and _path(getattr(args[0], 'co_filename', None)) in forbidden:
        audit.append(['exec', _path(args[0].co_filename)])
    elif name == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        raw = os.fsdecode(args[0])
        if _path(raw) in forbidden or not os.path.isabs(raw) and os.path.basename(raw) in forbidden_names:
            audit.append(['open', raw])
    elif name == 'pickle.find_class':
        audit.append(['pickle', str(args[0]) + '.' + str(args[1])])
sys.addaudithook(_hook)
sys.path[:0] = spec['path']
import artifact_acceptance_scenarios
result = artifact_acceptance_scenarios.run(spec)
site = os.path.realpath(spec['site']) if spec.get('site') else None
result['audit'] = audit
result['forbidden_modules'] = sorted(name for name in sys.modules
                                     if name in spec['forbidden_modules'] or name.startswith('markdown_it.'))
result['bindings'] = sorted(
    [name, type(getattr(module, '__loader__', None)).__name__, os.path.realpath(module.__file__)]
    for name, module in list(sys.modules.items())
    if site and getattr(module, '__file__', None) and os.path.realpath(module.__file__).startswith(site + os.sep))
result['source_modules'] = sorted(name for name, module in list(sys.modules.items())
                                  if _path(getattr(module, '__file__', None) or '') in forbidden)
print('ARTIFACT_ACCEPTANCE ' + json.dumps(result, sort_keys=True, default=repr))
'''


def build_wheel(directory, distribution, files):
    """Write a pure wheel with an exact RECORD; ``files`` maps archive paths to bytes."""
    name = distribution.replace('-', '_')
    info = f'{name}-1.0.dist-info'
    members = dict(files)
    members[f'{info}/METADATA'] = (f'Metadata-Version: 2.4\nName: {distribution}\nVersion: 1.0\n'
                                   'License-Expression: MIT\n').encode()
    members[f'{info}/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: artifact-acceptance\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    rows = [f'{path},sha256={urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode()},{len(data)}'
            for path, data in sorted(members.items())]
    members[f'{info}/RECORD'] = ('\n'.join([*rows, f'{info}/RECORD,,']) + '\n').encode()
    wheel = Path(directory) / f'{name}-1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(members.items()):
            entry = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(entry, data)
    return wheel


def install(wheel, site):
    """Install a pure wheel the way an installer lays it out on disk."""
    site = Path(site)
    site.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    return site


def source_files(definition):
    """Every admitted declaration, native and resource file the runtime must not read."""
    paths = {str(path.resolve()) for path in definition.contents if isinstance(path, Path)}
    return sorted(paths | {str(Path(definition.entry).resolve())})


def run(spec, *, timeout=120):
    """Run one scenario in a fresh isolated interpreter and return its result."""
    compiled = spec['mode'] == 'compiled' or spec['scenario'] == 'load'
    path = ([str(spec['site'])] if compiled else []) + [str(ROOT / 'src'), str(UNIT)]
    spec = {**spec, 'path': path, 'forbidden_modules': list(FORBIDDEN_MODULES)}
    completed = subprocess.run([sys.executable, '-I', '-c', _BOOT, json.dumps(spec, default=str)],
                               capture_output=True, text=True, timeout=timeout, cwd=spec.get('cwd'))
    lines = [line for line in completed.stdout.splitlines() if line.startswith('ARTIFACT_ACCEPTANCE ')]
    if completed.returncode or len(lines) != 1:
        raise AssertionError(f'scenario {spec["scenario"]} ({spec["mode"]}) failed '
                             f'with {completed.returncode}:\n{completed.stderr[-6000:]}')
    return json.loads(lines[0].removeprefix('ARTIFACT_ACCEPTANCE '))
