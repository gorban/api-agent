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


def _requirement_types():
    """Use an installed standards parser, including pip's bundled copy."""
    try:
        from packaging.requirements import Requirement, InvalidRequirement
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import Requirement, InvalidRequirement
        except ImportError as exc:
            raise OSError('Requirement validation needs packaging or pip') from exc
    return Requirement, InvalidRequirement


def _parsed_requirement(raw):
    """Validate manifest installer arguments before dependency preparation."""
    Requirement, InvalidRequirement = _requirement_types()
    if type(raw) is not str:
        raise ValueError('Dependency requirement must be a PEP 508 string')
    try:
        parsed = Requirement(raw)
    except InvalidRequirement as exc:
        raise ValueError('Dependency requirement is not a valid PEP 508 string') from None
    if parsed.url:
        raise ValueError('Direct URL requirements need explicit verification')
    return parsed


def requirements(context, *, collect=False, groups=(), selected_provider_manifests=()):
    dependencies = []
    library_root = getattr(context, 'workflow_base_root', None)
    projects = {}
    roots = (context.base_root, context.pack_root, library_root)
    for root in dict.fromkeys(Path(root).resolve() for root in roots if root is not None):
        manifest = root / 'pyproject.toml'
        if manifest.is_file():
            with manifest.open('rb') as handle:
                projects[root] = tomllib.load(handle).get('project', {})
    library_project = (projects.get(Path(library_root).resolve(), {})
                       if library_root is not None and getattr(context, 'workflow_library_files', None) is None
                       else {})
    normalize = lambda value: re.sub(r'[-_.]+', '-', value).lower()
    local_name = normalize(library_project.get('name', ''))
    expanded_extras = set()

    def add_requirement(raw):
        parsed = _parsed_requirement(raw)
        name = normalize(parsed.name)
        if name == 'api-agent':
            return
        if not local_name or name != local_name:
            dependencies.append(raw)
            return
        if parsed.marker and not parsed.marker.evaluate({'extra': ''}):
            return
        version = library_project.get('version')
        if not version or version not in parsed.specifier:
            raise ValueError('Selected source workflow library has an incompatible or missing version')
        optional = {}
        for key, value in library_project.get('optional-dependencies', {}).items():
            normalized = normalize(key)
            if normalized in optional:
                raise ValueError('Selected source workflow library has colliding extras')
            optional[normalized] = value
        for extra in sorted({normalize(extra) for extra in parsed.extras}):
            if extra not in optional:
                raise ValueError('Selected source workflow library has an unknown extra')
            if extra in expanded_extras:
                continue
            expanded_extras.add(extra)
            for child in optional[extra]:
                requirement = _parsed_requirement(child)
                if requirement.marker:
                    if not requirement.marker.evaluate({'extra': extra}):
                        continue
                    requirement.marker = None
                add_requirement(str(requirement))

    found_groups = set()
    # Admission already selected these roots; never search siblings or parents.
    for project in projects.values():
        selected = list(project.get('dependencies', []))
        optional = project.get('optional-dependencies', {})
        for group in groups:
            if group in optional:
                found_groups.add(group)
                selected.extend(optional[group])
        for requirement in selected:
            # Source loading supplies the base itself; install only external tools.
            add_requirement(requirement)
    if missing_groups := set(groups) - found_groups:
        raise ValueError(f'Unknown optional dependency group(s): {", ".join(sorted(missing_groups))}')
    # Callers pass only manifests for operation-active providers. Discovery and
    # collection do not select executable providers or prepare their packages.
    from api_agent.providers.distribution import ProviderDependencyManifest
    for manifest in selected_provider_manifests:
        if not isinstance(manifest, ProviderDependencyManifest):
            raise TypeError('Selected providers require dependency manifest descriptors')
        dependencies.extend(manifest.requirements())
    if collect:
        dependencies.extend(('pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4'))
    for requirement in dependencies:
        _parsed_requirement(requirement)
    return tuple(dict.fromkeys(dependencies))


def _installed_requirements(python, wanted):
    """Probe the target interpreter; a fingerprint alone cannot prove installation."""
    for requirement in wanted:
        _parsed_requirement(requirement)
    script = '''
import importlib.metadata as metadata
import json, sys
try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.markers import default_environment
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
    from pip._vendor.packaging.markers import default_environment
result = []
seen = set()
def check(raw, extra=''):
    req = Requirement(raw)
    if req.url:
        raise ValueError('Direct URL requirements need explicit verification')
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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError) as exc:
        raise OSError('Cannot verify dependencies in the target interpreter') from exc


def active_environment(environment):
    """Recognize the active venv through filesystem aliases as well as paths."""
    active = Path(sys.prefix)
    if active.resolve() == environment.resolve():
        return True
    try:
        return active.samefile(environment)
    except OSError:
        return False


def ensure_environment(context, argv, *, collect=False, groups=(), selected_provider_manifests=(), replace_process=False):
    """Prepare the venv; CLI owners may replace their process on handoff."""
    root = context.pack_root if context.external_pack else context.base_root
    environment = root / '.venv'
    python = environment / 'bin/python'
    if os.environ.get('API_AGENT_MISE') == '1' and (environment.exists() or environment.is_symlink()):
        # Do not let an older checkout environment bypass the selected runtime.
        try:
            probe = subprocess.run([str(python), '-I', '-c',
                'import sys; print(sys._base_executable)'], check=True,
                capture_output=True, text=True, timeout=30)
            matches = Path(probe.stdout.strip()).resolve() == Path(sys._base_executable).resolve()
        except (OSError, subprocess.SubprocessError):
            matches = False
        if not matches:
            raise OSError(f'{environment} uses a different or unavailable Python. '
                          'Move that environment aside, then retry ./launch.sh to recreate it '
                          'with the project Python. The existing environment has not been changed.')
    try:
        _requirement_types()
    except OSError:
        _prepare_requirement_parser(environment, python)
        return _relaunch(context, python, argv, replace_process=replace_process)
    wanted = requirements(context, collect=collect, groups=groups,
                          selected_provider_manifests=selected_provider_manifests)
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
    return _relaunch(context, python, argv, replace_process=replace_process)


def _relaunch(context, python, argv, *, replace_process=False):
    """Preserve the library subprocess contract unless the CLI owns this process."""
    pack_arguments = [] if getattr(context, 'show_pack_bootstrap', False) else [
        '--resource-pack-dir', str(context.pack_root)]
    # The parent already completed the gate; this is an internal interpreter handoff,
    # not another user launch. No command-line or environment approval flag exists.
    handoff = (
        "import sys; from pathlib import Path; "
        "base = Path(sys.argv[1]).resolve().parent; "
        "sys.path.insert(0, str(base / 'src')); "
        "from api_agent.runtime.bootstrap import main; "
        f"raise SystemExit(main(sys.argv[2:], base_root=base, _worktrees_verified=True, _replace_process={replace_process!r}))"
    )
    command = [str(python), '-c', handoff, str(context.base_root / 'launch.sh'),
               *pack_arguments,
               '--_launcher-root', str(getattr(context, 'launcher_root', None) or context.base_root), *argv]
    environment = os.environ.copy()
    if replace_process:
        os.execve(command[0], command, environment)
    return subprocess.call(command, env=environment)


_PARSER_PROBE = """
try:
    import packaging.requirements
except ImportError:
    try:
        import pip._vendor.packaging.requirements
    except ImportError:
        raise SystemExit(3)
"""


def _prepare_requirement_parser(environment, python):
    """Bootstrap only a fixed validator; no manifest arguments reach installers."""
    uv = shutil.which('uv')
    if not python.is_file():
        print(f'Preparing dependency validator in {environment}...', file=sys.stderr, flush=True)
        command = ([uv, 'venv', '--seed', '--python', sys.executable, str(environment)] if uv else
                   [sys.executable, '-m', 'venv', str(environment)])
        subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
    status = _requirement_parser_status(python)
    if status == 0:
        return
    if status != 3:
        raise OSError('Cannot verify the dependency validator in the target interpreter')
    if not uv:
        subprocess.run([str(python), '-m', 'ensurepip'], check=True,
                       stdout=sys.stderr, stderr=sys.stderr)
    command = ([uv, 'pip', 'install', '--python', str(python), 'packaging>=24,<27'] if uv else
               [str(python), '-m', 'pip', 'install', 'packaging>=24,<27'])
    subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)
    if _requirement_parser_status(python) != 0:
        raise OSError('Dependency validator remains unavailable after preparation')


def _requirement_parser_status(python):
    try:
        return subprocess.run([str(python), '-c', _PARSER_PROBE],
                              capture_output=True, timeout=20, check=False).returncode
    except (subprocess.TimeoutExpired, OSError) as error:
        raise OSError('Cannot verify the dependency validator in the target interpreter') from error
