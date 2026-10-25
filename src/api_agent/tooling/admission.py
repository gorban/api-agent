"""Capture bounded executable tool packages before operation acquisition."""
from dataclasses import dataclass, field, replace
from hashlib import sha256
from importlib import metadata, machinery
import importlib.util
import ast
import base64
from pathlib import Path
import sys
from types import MappingProxyType

from api_agent.workflows.definition import _read_bounded


MAX_TOOL_FILES = 512
MAX_TOOL_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_FILES = 2048
MAX_RUNTIME_BYTES = 32 * 1024 * 1024
MAX_DEPENDENCY_DISTRIBUTIONS = 128
MAX_DEPENDENCY_BYTES = 256 * 1024 * 1024


class ToolAdmissionError(ValueError):
    pass


class ToolDependencyMissing(ToolAdmissionError):
    pass


@dataclass(frozen=True)
class FactoryAdmission:
    factory: str
    entry: str
    sources: object
    dependencies: tuple
    identity: str
    interface: str = 'verified-read@1'
    assets: object = field(default_factory=dict, repr=False)


def _owned_origin(origin, owned):
    if type(origin) is not str or origin in ('built-in', 'frozen'):
        raise ToolAdmissionError('Installed tool dependency has no owned executable origin')
    resolved = str(Path(origin).resolve())
    if resolved not in owned:
        raise ToolAdmissionError('Tool dependency import origin is outside its admitted receipt')
    return resolved


def _owned_locations(locations, allowed):
    result = []
    for value in locations or ():
        location = Path(value).resolve()
        if str(location) not in allowed:
            raise ToolAdmissionError('Tool dependency package search location is outside its admitted receipt')
        result.append(str(location))
    return tuple(sorted(result))


def _validate_spec(spec, owned, allowed):
    if spec is None:
        raise ToolAdmissionError('Installed tool dependency has no importable owned module')
    locations = _owned_locations(spec.submodule_search_locations, allowed)
    origin = _owned_origin(spec.origin, owned) if spec.origin is not None else None
    if origin is not None and locations and not Path(origin).name.startswith('__init__.'):
        raise ToolAdmissionError('Tool dependency module advertises an unowned package shape')
    if origin is None and not locations:
        raise ToolAdmissionError('Tool dependency has no owned namespace location')
    return origin, locations



def _package_locations(name, owned):
    """Select receipt-owned package directories without broad parent exposure."""
    allowed = set()
    for value in owned:
        parents = list(Path(value).parents)
        package = next((parent for parent in parents if parent.name == name), None)
        if package is not None:
            allowed.update(str(parent) for parent in parents[:parents.index(package) + 1])
    return allowed

def _validate_import_origins(name, owned):
    allowed = _package_locations(name, owned)
    cold_specs = {}
    def cold_spec(module_name):
        if module_name not in cold_specs:
            parent, _, _ = module_name.rpartition('.')
            search = sys.path if not parent else cold_spec(parent).submodule_search_locations
            if search is None:
                raise ToolAdmissionError('Tool dependency parent is not an admitted package')
            spec = machinery.PathFinder.find_spec(module_name, search)
            _validate_spec(spec, owned, allowed)
            cold_specs[module_name] = spec
        return cold_specs[module_name]
    top = cold_spec(name)
    if name not in sys.modules:
        resolved = importlib.util.find_spec(name)
        if _validate_spec(resolved, owned, allowed) != _validate_spec(top, owned, allowed):
            raise ToolAdmissionError('Tool dependency resolved import origin differs from its admitted receipt')
    result = [('cold', name, _validate_spec(top, owned, allowed))]
    for module_name, module in tuple(sys.modules.items()):
        if module_name != name and not module_name.startswith(name + '.'):
            continue
        if module is None:
            raise ToolAdmissionError('Tool dependency has an unavailable warm module')
        spec = getattr(module, '__spec__', None)
        if spec is None or spec.name != module_name:
            raise ToolAdmissionError('Tool dependency warm module identity changed')
        origin, locations = _validate_spec(spec, owned, allowed)
        if (origin, locations) != _validate_spec(cold_spec(module_name), owned, allowed):
            raise ToolAdmissionError('Tool dependency warm and resolved origins differ')
        filename = getattr(module, '__file__', None)
        if origin is not None and _owned_origin(filename, owned) != origin:
            raise ToolAdmissionError('Tool dependency warm module origin changed')
        actual_paths = _owned_locations(getattr(module, '__path__', ()), allowed)
        if actual_paths != locations:
            raise ToolAdmissionError('Tool dependency warm package search locations changed')
    # Warming a receipt-owned module is allowed without changing its identity.
    return tuple(result)


class _ReceiptSourceLoader(machinery.SourceFileLoader):
    def __init__(self, name, path, expected):
        super().__init__(name, path)
        self.expected = expected

    def get_code(self, fullname):
        path = Path(self.path)
        source = _read_bounded(path, path.parent)
        if sha256(source).hexdigest() != self.expected:
            raise ToolAdmissionError('Tool dependency source changed before import')
        # Compile receipt-verified source rather than consulting an unowned pyc.
        return compile(source, self.path, 'exec', dont_inherit=True)


def admitted_dependency_spec(admission, fullname, path):
    root = fullname.partition('.')[0]
    owned = None
    for _, receipt in admission.dependencies:
        for row in receipt or ():
            if len(row) == 2 and row[0] == 'owned-import-files':
                owned = dict(dict(row[1]).get(root, ())) or owned
    if owned is None:
        return None
    allowed = _package_locations(root, owned)
    if path is not None:
        _owned_locations(path, allowed)
    spec = machinery.PathFinder.find_spec(fullname, path)
    origin, _ = _validate_spec(spec, owned, allowed)
    if origin is not None:
        if isinstance(spec.loader, machinery.SourceFileLoader):
            spec.loader = _ReceiptSourceLoader(fullname, origin, owned[origin])
        elif isinstance(spec.loader, machinery.ExtensionFileLoader):
            if sha256(Path(origin).read_bytes()).hexdigest() != owned[origin]:
                raise ToolAdmissionError('Tool dependency executable changed before import')
        else:
            raise ToolAdmissionError('Tool dependency loader is outside its admitted receipt')
    return spec


def _dependency(name, selected_extras=None):
    if name == 'api_agent':
        # The executing runtime is the trusted core dependency in both checkout
        # and installed modes. Pin its actual executable bytes, not a different
        # installation that happens to supply the distribution metadata.
        root = Path(__file__).absolute().parents[1].resolve(strict=True)
        digest, total = sha256(), 0
        paths = sorted(root.rglob('*.py'))
        if len(paths) > MAX_RUNTIME_FILES:
            raise ToolAdmissionError('Runtime dependency exceeds admission limits')
        for path in paths:
            body = _read_bounded(path, root)
            total += len(body)
            if total > MAX_RUNTIME_BYTES:
                raise ToolAdmissionError('Runtime dependency exceeds admission limits')
            digest.update(path.relative_to(root).as_posix().encode() + b'\0' + body)
        if root.parent.name == 'src' and (root.parent.parent / 'pyproject.toml').is_file():
            project = _read_bounded(root.parent.parent / 'pyproject.toml', root.parent.parent)
            version = 'source:' + sha256(project).hexdigest()
        else:
            version = metadata.version('api-agent')
        owned = {str(path.resolve()): sha256(_read_bounded(path, root)).hexdigest() for path in paths}
        origins = _validate_import_origins('api_agent', owned)
        return (('api-agent-runtime', version, digest.hexdigest(), origins),
                ('owned-import-files', (('api_agent', tuple(sorted(owned.items()))),)))
    packages = metadata.packages_distributions()
    initial = packages.get(name, ())
    if not initial:
        raise ToolDependencyMissing('Tool dependency ' + name + ' has no installed distribution identity')
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    active_extras = {key: set(value) for key, value in (selected_extras or {}).items()}
    pending, visited, rows, owned_by_root, ownership = list(initial), {}, {}, {}, {}
    total = 0
    while pending:
        distribution_name = pending.pop()
        normalized = canonicalize_name(distribution_name)
        extras = active_extras.get(normalized, set())
        if normalized in visited and extras <= visited[normalized]:
            continue
        if len(visited) >= MAX_DEPENDENCY_DISTRIBUTIONS:
            raise ToolAdmissionError('Tool dependency closure exceeds admission limits')
        visited[normalized] = set(extras)
        distribution = metadata.distribution(distribution_name)
        record = distribution.read_text('RECORD')
        if record is None:
            raise ToolAdmissionError('Tool dependency has no installed file receipt')
        roots = {root for root, owners in packages.items()
                 if any(canonicalize_name(owner) == normalized for owner in owners)}
        if distribution_name in initial:
            roots.add(name)
        if not roots:
            raise ToolAdmissionError('Tool dependency has no distribution-owned import roots')
        digest, executable = sha256(), set()
        for entry in distribution.files or ():
            logical = entry.as_posix()
            root = logical.partition('/')[0].split('.')[0]
            if root not in roots or logical.endswith('.pyc'):
                continue
            if entry.hash is None:
                raise ToolAdmissionError('Installed tool dependency has an unhashed file')
            path = Path(distribution.locate_file(entry))
            if path.is_symlink() or not path.is_file():
                raise ToolAdmissionError('Installed tool dependency file is unavailable')
            body = path.read_bytes()
            total += len(body)
            if total > MAX_DEPENDENCY_BYTES or entry.hash.mode != 'sha256':
                raise ToolAdmissionError('Installed tool dependency exceeds admission limits')
            actual = base64.urlsafe_b64encode(sha256(body).digest()).rstrip(b'=').decode()
            if actual != entry.hash.value:
                raise ToolAdmissionError('Installed tool dependency differs from its receipt')
            physical = str(path.resolve())
            if physical in ownership and ownership[physical] != normalized:
                raise ToolAdmissionError('Tool dependency executable has ambiguous receipt ownership')
            ownership[physical] = normalized
            if logical.endswith(('.py', '.so', '.pyd')):
                executable.add(root)
                owned_by_root.setdefault(root, {})[physical] = sha256(body).hexdigest()
            digest.update(logical.encode() + b'\0' + body)
        if roots - executable:
            raise ToolAdmissionError('Tool dependency receipt has no owned executable files')
        rows[normalized] = (distribution.metadata['Name'], distribution.version,
                            sha256(record.encode()).hexdigest(), digest.hexdigest())
        for raw in getattr(distribution, 'requires', ()) or ():
            requirement = Requirement(raw)
            if requirement.url:
                raise ToolAdmissionError('Tool dependency requires an unadmitted direct URL')
            if requirement.marker and not any(requirement.marker.evaluate({'extra': extra})
                                              for extra in ('', *sorted(extras))):
                continue
            try:
                child = metadata.distribution(requirement.name)
            except metadata.PackageNotFoundError:
                raise ToolDependencyMissing('Transitive tool dependency ' + requirement.name + ' is not installed') from None
            if requirement.specifier and child.version not in requirement.specifier:
                raise ToolAdmissionError('Installed transitive tool dependency has an incompatible version')
            child_name = canonicalize_name(requirement.name)
            active_extras.setdefault(child_name, set()).update(requirement.extras)
            pending.append(requirement.name)
    origins = tuple((root, _validate_import_origins(root, owned)) for root, owned in sorted(owned_by_root.items()))
    owned = tuple((root, tuple(sorted(files.items()))) for root, files in sorted(owned_by_root.items()))
    return tuple(sorted(rows.values())) + (('import-origins', origins), ('owned-import-files', owned))



def _factory_binding_changed(tree, factory):
    """Inspect bindings executed at module scope, excluding function-local names."""
    if any(isinstance(node, ast.Global) and 'create_tool' in node.names for node in ast.walk(tree)):
        return True
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not factory and node.name == 'create_tool':
                return True
            # Defaults, annotations, bases and decorators execute in the outer
            # scope. Function and class body bindings use their own namespace.
            pending.extend(child for field, value in ast.iter_fields(node) if field != 'body'
                           for child in (value if isinstance(value, list) else (value,))
                           if isinstance(child, ast.AST))
            continue
        if isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            pending.extend(value for value in node.args.kw_defaults if value is not None)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((alias.asname or alias.name.split('.')[0]) in ('create_tool', '*') for alias in node.names):
                return True
        if isinstance(node, ast.Name) and node.id == 'create_tool' and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == 'create_tool':
            return True
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == 'create_tool':
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == 'create_tool':
            return True
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            target = node.value
            while isinstance(target, (ast.Attribute, ast.Subscript)):
                target = target.value
            if isinstance(target, ast.Name) and target.id == 'create_tool':
                return True
        if isinstance(node, ast.comprehension):
            pending.extend((node.iter, *node.ifs))
        else:
            pending.extend(ast.iter_child_nodes(node))
    return False

def _check_factory_source(body, filename):
    """Admit the bounded direct factory signature without executing its module."""
    try:
        tree = ast.parse(body, filename=filename)
        compile(tree, filename, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError):
        raise ToolAdmissionError('Invalid selected tool source') from None
    definitions = [node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and node.name == 'create_tool']
    if len(definitions) != 1 or not isinstance(definitions[0], ast.FunctionDef):
        raise ToolAdmissionError('Tool entry requires a direct synchronous create_tool factory')
    function = definitions[0]
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            raise ToolAdmissionError('Tool factory must return an owner synchronously')
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    arguments = function.args
    if (function.decorator_list or len(arguments.posonlyargs) + len(arguments.args) != 2
            or arguments.vararg is not None or arguments.kwarg is not None or arguments.kwonlyargs):
        raise ToolAdmissionError('Tool factory must accept exactly local_config and services positionally')
    if _factory_binding_changed(tree, function):
        raise ToolAdmissionError('Tool factory cannot be rebound after its declaration')
    return tree

def capture_tool_factory(factory: Path, factory_name: str, owner_root: Path) -> FactoryAdmission:
    """Capture the selected package, including sibling modules and initializers.

    Third-party imports must be installed distributions; their package receipts
    are pinned and checked again before executing the captured package.
    """
    factory, owner_root = Path(factory).absolute(), Path(owner_root).absolute()
    if factory_name != 'create_tool' or factory.suffix != '.py' or not factory.is_relative_to(owner_root):
        raise ToolAdmissionError('Invalid selected tool factory')
    package = factory.parent
    # The containing package subtree is finite; every relative import consumes
    # these bytes rather than consulting the source directory during execution.
    sources, imported, total = {}, set(), 0
    paths = sorted(package.rglob('*.py'))
    if len(paths) > MAX_TOOL_FILES:
        raise ToolAdmissionError('Tool executable closure is too large')
    for path in paths:
        body = _read_bounded(path, owner_root)
        total += len(body)
        if total > MAX_TOOL_BYTES:
            raise ToolAdmissionError('Tool executable closure is too large')
        relative = path.relative_to(package).as_posix()
        sources[relative] = body
        try:
            tree = ast.parse(body, filename=relative)
        except (SyntaxError, ValueError):
            raise ToolAdmissionError('Invalid selected tool source') from None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level > len(Path(relative).parts):
                    raise ToolAdmissionError('Tool import escapes its captured package')
                if node.level == 0 and node.module:
                    imported.add(node.module.split('.')[0])
    if factory.relative_to(package).as_posix() not in sources:
        raise ToolAdmissionError('Selected factory is absent from captured closure')
    _check_factory_source(sources[factory.relative_to(package).as_posix()], str(factory))
    dependencies = []
    local_roots = {name.partition('/')[0].removesuffix('.py') for name in sources}
    for name in sorted(imported - sys.stdlib_module_names):
        if name in local_roots:
            raise ToolAdmissionError('Tool package imports must use relative names')
        dependencies.append((name, None))
    assets = {}
    for directory in dict.fromkeys((owner_root, package)):
        for path in sorted(directory.iterdir()):
            if path.name.upper().startswith(('LICENSE', 'NOTICE', 'COPYING')) and path.is_file():
                assets[path] = _read_bounded(path, owner_root)
                total += len(assets[path])
                if total > MAX_TOOL_BYTES:
                    raise ToolAdmissionError('Tool executable closure is too large')
    manifest = package / 'provider-dependencies.toml'
    if manifest.exists():
        assets[manifest] = _read_bounded(manifest, owner_root)
    sources, dependencies, assets = MappingProxyType(sources), tuple(dependencies), MappingProxyType(assets)
    return FactoryAdmission(factory_name, factory.relative_to(package).as_posix(),
                            sources, dependencies, _identity(sources, dependencies, assets), assets=assets)


def _identity(sources, dependencies, assets):
    fingerprint = sha256()
    for name, body in sorted(sources.items()):
        fingerprint.update(name.encode() + b'\0' + body + b'\0')
    for name, body in sorted((path.name, body) for path, body in assets.items()):
        fingerprint.update(name.encode() + b'\0' + body + b'\0')
    fingerprint.update(repr(dependencies).encode())
    return fingerprint.hexdigest()


def _check_admission(admission):
    if not isinstance(admission, FactoryAdmission) or admission.interface != 'verified-read@1':
        raise ToolAdmissionError('Tool factory admission is required')
    if (admission.factory != 'create_tool' or admission.entry not in admission.sources
            or admission.identity != _identity(admission.sources, admission.dependencies, admission.assets)):
        raise ToolAdmissionError('Tool executable admission identity changed')
    _check_factory_source(admission.sources[admission.entry], admission.entry)



def _manifest_requirements(admission):
    from packaging.requirements import Requirement
    from api_agent.tooling.services import captured_tool_manifest
    requirements = []
    for path, body in admission.assets.items():
        if path.name != 'provider-dependencies.toml':
            continue
        manifest = captured_tool_manifest(path, body, 'selected-tool')
        for raw in manifest.dependencies:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                requirements.append(requirement)
    return tuple(requirements)


def _manifest_imports(requirements):
    """Resolve only prepared distributions, never import code or install packages."""
    from packaging.utils import canonicalize_name
    imports = set()
    packages = metadata.packages_distributions() if requirements else {}
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise ToolDependencyMissing('Declared tool dependency ' + name + ' is not installed; prepare selected dependency manifests') from None
        if requirement.specifier and distribution.version not in requirement.specifier:
            raise ToolAdmissionError('Declared tool dependency ' + name + ' does not satisfy its captured manifest')
        roots = {root for root, owners in packages.items()
                 if any(canonicalize_name(owner) == name for owner in owners)}
        if not roots:
            raise ToolAdmissionError('Declared tool dependency ' + name + ' has no known executable import roots')
        imports.update(roots)
    return tuple(sorted(imports))


def _manifest_extras(requirements):
    from packaging.utils import canonicalize_name
    extras = {}
    for requirement in requirements:
        extras.setdefault(canonicalize_name(requirement.name), set()).update(requirement.extras)
    return extras


def prepare_tool_admission(admission):
    """Pin installed requirements after dependency preparation, before imports."""
    _check_admission(admission)
    dependencies, missing = [], []
    requirements = _manifest_requirements(admission)
    extras = _manifest_extras(requirements)
    requested = dict(admission.dependencies)
    for name in _manifest_imports(requirements):
        requested.setdefault(name, None)
    for name, expected in sorted(requested.items()):
        try:
            actual = _dependency(name, extras)
        except ToolDependencyMissing:
            missing.append(name)
            continue
        if expected is not None and actual != expected:
            raise ToolAdmissionError('Installed tool dependency identity changed')
        dependencies.append((name, actual))
    if missing:
        raise ToolAdmissionError('Missing tool dependencies: ' + ', '.join(missing)
                                 + '; prepare selected dependency manifests')
    dependencies = tuple(dependencies)
    return replace(admission, dependencies=dependencies,
                   identity=_identity(admission.sources, dependencies, admission.assets))


def verify_tool_dependencies(admission):
    _check_admission(admission)
    requirements = _manifest_requirements(admission)
    extras = _manifest_extras(requirements)
    if not set(_manifest_imports(requirements)) <= dict(admission.dependencies).keys():
        raise ToolAdmissionError('Captured tool manifest executable roots are not admitted')
    for name, expected in admission.dependencies:
        if expected is None:
            raise ToolAdmissionError('Tool dependencies require preparation before factory acquisition')
        if _dependency(name, extras) != expected:
            raise ToolAdmissionError('Installed tool dependency identity changed')
