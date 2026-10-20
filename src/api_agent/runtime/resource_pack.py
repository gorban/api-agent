"""Resource-pack ownership and paths, resolved without relying on CWD."""

from dataclasses import dataclass
from importlib import metadata
from importlib.resources import files
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
import tomllib


class ResourcePackError(ValueError):
    """A pack cannot provide a valid launch context."""


@dataclass(frozen=True)
class LaunchContext:
    base_root: Path
    pack_root: Path
    owner_root: Path
    name: str
    pack_id: str
    config_path: Path
    defaults: Mapping[str, object]
    tests: Mapping[str, tuple[Path, ...]]
    packaged_demo: bool = False
    launcher_root: Path | None = None
    show_pack_bootstrap: bool = False
    credential_role_fields: tuple[str, ...] = ()
    identity_factory_path: Path | None = None
    identity_factory_name: str | None = None
    production_paths: tuple[Path, ...] = ()
    bootstrap_source: str | None = None
    bootstrap_explicit: bool = False
    service_name: str = "api-agent"
    provider_config_paths: tuple[Path, ...] = ()
    shared_dotenv_paths: tuple[Path, ...] = ()
    workflow_root: str = 'base'
    workflow_entry: Path | None = None
    workflow_replacements: tuple[tuple[str, Path, str], ...] = ()
    workflow_base_root: Path | None = None
    workflow_library_files: frozenset[str] | None = None

    @property
    def dotenv_path(self) -> Path:
        return (self.pack_root if self.external_pack else self.base_root) / ".env"

    @property
    def anchors(self) -> dict[str, Path]:
        return {"base_root": self.base_root, "pack_root": self.pack_root, "owner_root": self.owner_root}

    @property
    def external_pack(self) -> bool:
        return self.pack_root != self.base_root / "demo" and not self.packaged_demo

    def suite_paths(self, mode: str) -> tuple[Path, ...]:
        """Return pack suite paths; collection also adds shared/provider integrations."""
        if mode not in {"unit", "integration", "e2e"}:
            raise ResourcePackError(f"Unknown test mode: {mode}")
        paths = self.tests.get(mode, ())
        if mode == "unit":
            paths = (self.base_root / "tests/unit",) + paths
        return tuple(dict.fromkeys(paths))


def _relative_path(value: object, root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise ResourcePackError(f"{field} must be a nonempty relative path")
    return (root / value).resolve()


def load_launch_context(
    base_root: Path,
    resource_pack_dir: Path | None = None,
    launcher_root: Path | None = None,
) -> LaunchContext:
    base = Path(base_root).resolve()
    if not base.is_dir():
        raise ResourcePackError(f"Base api-agent directory does not exist: {base}")
    packaged_demo = False
    if resource_pack_dir is not None:
        pack = Path(resource_pack_dir).resolve()
    elif launcher_root is not None and Path(launcher_root).resolve() != base:
        pack = Path(launcher_root).resolve()
    else:
        pack = base / "demo"
        if not pack.is_dir():
            # Wheels include the same root demo under package data.
            pack = Path(str(files("api_agent").joinpath("demo"))).resolve()
            packaged_demo = True
    manifest = pack / "pack.toml"
    try:
        with manifest.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ResourcePackError(f"Cannot read resource-pack declaration: {manifest}") from exc
    allowed = {"version", "id", "owner", "config", "tests", "defaults", "authentication", "production", "service_name", "providers", "configuration", "workflow"}
    if set(data) - allowed:
        raise ResourcePackError(f"Unknown fields in resource-pack declaration: {manifest}")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ResourcePackError(f"Unsupported resource-pack version in {manifest}; expected 1")
    pack_id = data.get("id")
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ResourcePackError(f"Resource-pack id must be a nonempty string: {manifest}")
    service_name = data.get("service_name", "api-agent")
    if (type(service_name) is not str or not service_name
            or any(not 32 <= ord(char) <= 126 for char in service_name)):
        raise ResourcePackError("Resource-pack service_name must be nonempty printable ASCII")
    owner = _relative_path(data.get("owner", "."), pack, "owner")
    if packaged_demo:
        owner = base
    if not owner.is_dir():
        raise ResourcePackError(f"Resource-pack owner directory does not exist: {owner}")
    config = _relative_path(data.get("config"), pack, "config")
    if not config.is_file():
        raise ResourcePackError(f"Resource-pack configuration does not exist: {config}")
    workflow = data.get('workflow')
    workflow_root = 'base'
    workflow_entry = None
    workflow_replacements = ()
    workflow_base_root = None
    workflow_library_files = None
    if workflow is not None:
        if (type(workflow) is not dict or set(workflow) - {'version', 'root', 'entry', 'replacements', 'library'}
                or not {'version', 'root', 'entry'} <= set(workflow)
                or type(workflow.get('version')) is not int or workflow['version'] != 2):
            raise ResourcePackError('workflow must declare version=2, root and entry')
        library = workflow.get('library')
        if library is not None:
            workflow_base_root, workflow_library_files = _workflow_library(pack, library)
        roots = {'base': workflow_base_root or base, 'pack': pack}
        root_id = workflow.get('root')
        if type(root_id) is not str or root_id not in roots:
            raise ResourcePackError('workflow.root must be base or pack')
        workflow_root = root_id
        workflow_entry = _workflow_path(workflow.get('entry'), roots[root_id], 'workflow.entry')
        raw_replacements = workflow.get('replacements', [])
        if type(raw_replacements) is not list:
            raise ResourcePackError('workflow.replacements must be an array of tables')
        selected = []
        seen = set()
        for replacement in raw_replacements:
            if type(replacement) is not dict or set(replacement) != {'target_id', 'entry', 'root_id'}:
                raise ResourcePackError('workflow.replacements entries require target_id, entry and root_id')
            target = replacement['target_id']
            replacement_root = replacement['root_id']
            if (type(target) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', target)
                    or target in seen):
                raise ResourcePackError('workflow.replacements target_id must be a unique identifier')
            if type(replacement_root) is not str or replacement_root not in roots:
                raise ResourcePackError('workflow.replacements root_id must be base or pack')
            seen.add(target)
            checked = _workflow_selected_path(replacement['entry'], roots[replacement_root],
                                              'workflow.replacements entry')
            selected.append((target, checked.relative_to(roots[replacement_root]), replacement_root))
        workflow_replacements = tuple(selected)
    defaults = data.get("defaults", {})
    providers = data.get('providers', {})
    if not isinstance(providers, dict) or set(providers) - {'configs'}:
        raise ResourcePackError('providers must contain only a configs array')
    configs = providers.get('configs', [])
    if not isinstance(configs, list):
        raise ResourcePackError('providers.configs must be an array of declaration paths')
    provider_configs = []
    for entry in configs:
        source = _relative_path(entry, pack, 'providers.configs')
        if source.suffix != '.toml' or not source.is_file():
            raise ResourcePackError('providers.configs must name existing TOML declarations')
        provider_configs.append(source)
    configuration = data.get("configuration", {})
    if not isinstance(configuration, dict) or set(configuration) - {"dotenv"}:
        raise ResourcePackError("configuration must contain only a dotenv array")
    dotenv_paths = configuration.get("dotenv", [])
    if not isinstance(dotenv_paths, list):
        raise ResourcePackError("configuration.dotenv must be an array of relative paths")
    shared_dotenv_paths = tuple(dict.fromkeys(
        _relative_path(entry, pack, "configuration.dotenv") for entry in dotenv_paths))
    declared_tests = data.get("tests", {})
    if not isinstance(defaults, dict) or not isinstance(declared_tests, dict):
        raise ResourcePackError("Resource-pack defaults and tests must be TOML tables")
    if set(declared_tests) - {"unit", "integration", "e2e"}:
        raise ResourcePackError("Resource-pack tests contains an unknown test mode")
    production = data.get("production", [])
    if not isinstance(production, list):
        raise ResourcePackError('Resource-pack production must list relative source directories')
    production_paths = []
    for entry in production:
        source = _relative_path(entry, pack, 'production')
        if not source.is_relative_to(pack.resolve()) or not source.is_dir():
            raise ResourcePackError('Production source directories must exist inside the resource pack')
        production_paths.append(source)
    authentication = data.get("authentication", {})
    if not isinstance(authentication, dict) or set(authentication) - {"credential_role_fields", "identity"}:
        raise ResourcePackError("Resource-pack authentication contains unknown or invalid fields")
    role_fields = authentication.get("credential_role_fields", [])
    if (not isinstance(role_fields, list)
            or any(not isinstance(name, str) or not name.isascii() or not name.isidentifier()
                   for name in role_fields)
            or len(set(role_fields)) != len(role_fields)):
        raise ResourcePackError("authentication.credential_role_fields must list unique configuration field names")
    identity = authentication.get("identity")
    factory_path = factory_name = None
    if identity is not None:
        if (not isinstance(identity, dict) or set(identity) != {"version", "path", "factory"}
                or type(identity["version"]) is not int or identity["version"] != 1):
            raise ResourcePackError("authentication.identity must declare version=1, path and factory")
        path = identity["path"]
        factory_name = identity["factory"]
        if (not isinstance(path, str) or not path.strip() or Path(path).is_absolute()
                or ".." in Path(path).parts or Path(path).suffix != ".py"):
            raise ResourcePackError("authentication.identity.path must be a pack-relative Python file")
        factory_path = (pack / path).resolve()
        if not factory_path.is_relative_to(pack.resolve()) or not factory_path.is_file():
            raise ResourcePackError("authentication.identity.path must exist inside the resource pack")
        if (not isinstance(factory_name, str) or not factory_name.isascii()
                or not factory_name.isidentifier()):
            raise ResourcePackError("authentication.identity.factory must name a Python identifier")
        if not any(factory_path.is_relative_to(source) for source in
                   [owner / 'src', *production_paths]):
            raise ResourcePackError("authentication.identity.path must be included in production coverage directories")
    tests: dict[str, tuple[Path, ...]] = {}
    for mode, paths in declared_tests.items():
        if not isinstance(paths, list):
            raise ResourcePackError(f"tests.{mode} must be an array of relative paths")
        tests[mode] = tuple(_relative_path(path, owner, f"tests.{mode}") for path in paths)
    return LaunchContext(
        base, pack, owner, owner.name, pack_id, config,
        MappingProxyType(dict(defaults)), MappingProxyType(tests), packaged_demo,
        Path(launcher_root).resolve() if launcher_root is not None else base,
        resource_pack_dir is None and (launcher_root is None or Path(launcher_root).resolve() == base),
        tuple(role_fields),
        factory_path, factory_name,
        tuple(dict.fromkeys(production_paths)),
        service_name=service_name,
        provider_config_paths=tuple(dict.fromkeys(provider_configs)),
        shared_dotenv_paths=shared_dotenv_paths,
        workflow_root=workflow_root,
        workflow_entry=workflow_entry,
        workflow_replacements=workflow_replacements,
        workflow_base_root=workflow_base_root,
        workflow_library_files=workflow_library_files,
    )


def _workflow_path(value: object, root: Path, field: str) -> Path:
    if type(value) is not str or Path(value).suffixes != ['.md', '.html']:
        raise ResourcePackError(f'{field} must be a relative .md.html declaration')
    return _workflow_selected_path(value, root, field)


def _workflow_selected_path(value: object, root: Path, field: str) -> Path:
    if type(value) is not str or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ResourcePackError(f'{field} must be a relative file')
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ResourcePackError(f'{field} must exist inside its declared root')
    return path


def _workflow_library(pack: Path, declaration: object) -> tuple[Path, frozenset[str] | None]:
    """Resolve a declared sibling source or an installed distribution without code import."""
    if (type(declaration) is not dict or set(declaration) != {'source', 'distribution', 'resource'}
            or any(type(declaration[key]) is not str or not declaration[key]
                   for key in ('source', 'distribution', 'resource'))):
        raise ResourcePackError('workflow.library requires source, distribution and resource')
    source, distribution_name, resource = (declaration[key] for key in ('source', 'distribution', 'resource'))
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', distribution_name) is None:
        raise ResourcePackError('workflow.library.distribution must be a distribution name')
    source_path = Path(source)
    if (source_path.is_absolute() or source_path.parts[:1] != ('..',)
            or any(part == '..' for part in source_path.parts[1:])
            or not source_path.name):
        raise ResourcePackError('workflow.library.source must select a sibling directory')
    raw_candidate = pack.parent.joinpath(*source_path.parts[1:])
    if _has_symlink_component(raw_candidate, pack.parent):
        raise ResourcePackError('workflow.library.source contains a symlink alias')
    candidate = raw_candidate.resolve()
    if not candidate.is_relative_to(pack.parent.resolve()) or candidate.is_relative_to(pack):
        raise ResourcePackError('workflow.library.source escapes sibling roots')
    resource_path = Path(resource)
    if (resource_path.is_absolute() or not resource_path.parts
            or any(part in ('', '.', '..') for part in resource_path.parts)
            or '\\' in resource or resource_path.as_posix() != resource):
        raise ResourcePackError('workflow.library.resource must be a normalized relative directory')
    if candidate.exists():
        if not candidate.is_dir():
            raise ResourcePackError('workflow.library.source exists but is not a regular directory')
        return candidate, None
    selected = list(metadata.distributions(name=distribution_name))
    if len(selected) > 1:
        identities = [_distribution_identity(item) for item in selected]
        if identities[0] is not None and all(item == identities[0] for item in identities):
            selected = selected[:1]
    if len(selected) != 1:
        raise ResourcePackError('workflow.library.distribution must identify exactly one installed distribution')
    distribution = selected[0]
    normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()
    if normalize(distribution.metadata['Name']) != normalize(distribution_name):
        raise ResourcePackError('workflow.library.distribution identity mismatch')
    inventory = distribution.files
    if inventory is None:
        raise ResourcePackError('workflow.library.distribution has no file inventory')
    raw_root = Path(str(distribution.locate_file('')))
    located_root = raw_root.resolve()
    raw_located = Path(str(distribution.locate_file(resource)))
    located = raw_located.resolve()
    if (not raw_located.is_relative_to(raw_root)
            or _has_symlink_component(raw_located, raw_root)
            or not located.is_relative_to(located_root) or not located.is_dir()):
        raise ResourcePackError('workflow.library.resource is not an installed directory')
    owned = set()
    for item in inventory:
        item_path = Path(str(item))
        # Wheel RECORD also owns scripts outside site-packages. Only canonical
        # parent-prefixed entries resolving outside the install root are irrelevant.
        if ('..' in item_path.parts and not item_path.is_absolute()
                and '\\' not in str(item) and item_path.parts[0] == '..'
                and not Path(str(distribution.locate_file(item))).resolve().is_relative_to(located_root)):
            continue
        if (item_path.is_absolute() or any(part in ('', '.', '..') for part in item_path.parts)
                or '\\' in str(item)):
            raise ResourcePackError('workflow.library.distribution has an invalid file inventory')
        raw_physical = Path(str(distribution.locate_file(item)))
        if raw_physical.is_relative_to(raw_located):
            physical = raw_physical.resolve()
            if (_has_symlink_component(raw_physical, raw_root)
                    or not physical.is_relative_to(located)
                    or not raw_physical.is_file()):
                raise ResourcePackError('workflow.library.distribution has an invalid owned file')
            owned.add(raw_physical.relative_to(raw_located).as_posix())
    if not owned:
        raise ResourcePackError('workflow.library.resource has no distribution-owned files')
    return located, frozenset(owned)


def _distribution_identity(distribution):
    """Prove duplicate discovery using physical metadata and exact inventory."""
    try:
        # Only a filesystem provider exposes a verifiable metadata source.
        # PathDistribution stores that source in _path; custom providers without
        # this representation cannot prove duplicate discovery and fail closed.
        if not isinstance(distribution, metadata.PathDistribution):
            return None
        provider_root = Path(distribution._path)
        provider_stat = provider_root.stat()
        inventory = distribution.files
        if inventory is None:
            return None
        metadata_files = [item for item in inventory
                          if str(item).endswith('.dist-info/METADATA')]
        record_files = [item for item in inventory
                        if str(item).endswith('.dist-info/RECORD')]
        if len(metadata_files) != 1 or len(record_files) != 1:
            return None
        paths = [Path(str(distribution.locate_file(item)))
                 for item in (*metadata_files, *record_files)]
        if not all(path.samefile(provider_root / name)
                   for path, name in zip(paths, ('METADATA', 'RECORD'))):
            return None
        physical = tuple((path.stat().st_dev, path.stat().st_ino, path.read_bytes())
                         for path in paths)
        return ((provider_stat.st_dev, provider_stat.st_ino), physical, distribution.read_text('METADATA'), distribution.read_text('RECORD'),
                str(distribution.metadata),
                tuple((str(item), str(distribution.locate_file(item).resolve()),
                       str(item.hash), item.size) for item in inventory))
    except (OSError, AttributeError, ValueError):
        return None


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Inspect the declared path spelling before resolving any intermediate alias."""
    if not path.is_relative_to(root):
        return True
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _workflow_base(context) -> Path:
    return getattr(context, 'workflow_base_root', None) or context.base_root


def load_selected_definition(context):
    """Load the selected complete graph without initializing providers or executing nodes."""
    from api_agent.workflows.definition import Replacement, _read_bounded, load_definition
    base = _workflow_base(context)
    entry = context.workflow_entry or base / 'flow.md.html'
    if not entry.is_file():
        raise ResourcePackError('The bundled application workflow entry is unavailable')
    replacements = tuple(Replacement(*item) for item in context.workflow_replacements)
    owner = context.pack_root if context.workflow_root == 'pack' else base
    from api_agent.workflows.document_format import canonical_executable_bytes
    heading = canonical_executable_bytes(_read_bounded(entry, owner), path=entry).split(b'\n', 1)[0]
    if heading == b'# Workflow v3':
        from api_agent.workflows.stateful import load_stateful_definition
        definition = load_stateful_definition(entry, base_root=base,
                                              pack_root=context.pack_root, replacements=replacements,
                                              entry_root=context.workflow_root)
    else:
        definition = load_definition(entry, base_root=base,
                                     pack_root=context.pack_root, replacements=replacements,
                                     entry_root=context.workflow_root)
    owned = getattr(context, 'workflow_library_files', None)
    if owned is not None:
        selected = {path.relative_to(base).as_posix() for path in definition.contents
                    if isinstance(path, Path) and path.is_relative_to(base)}
        if not selected <= owned:
            raise ResourcePackError('Selected workflow file is not owned by installed distribution')
    return definition


def load_workflow(context, registry, conditions=None, *, definition=None, config=None, services=None,
                  reducers=None, checkpointer=None, journal=None):
    """Join the selected immutable definition to an independently owned catalog."""
    from api_agent.workflows import Workflow
    from api_agent.workflows.definition import Replacement, WorkflowDefinition
    from api_agent.workflows.stateful import StatefulDefinition, StatefulWorkflow
    if definition is None:
        definition = load_selected_definition(context)
    base = _workflow_base(context)
    expected_entry = context.workflow_entry or base / 'flow.md.html'
    expected_replacements = tuple(Replacement(*item) for item in context.workflow_replacements)
    if (not isinstance(definition, (WorkflowDefinition, StatefulDefinition)) or definition.entry != expected_entry
            or definition.entry_root != context.workflow_root
            or definition.roots != {'base': base, 'pack': context.pack_root}
            or definition.replacements != expected_replacements):
        raise ResourcePackError('Workflow definition does not match selected pack graph')
    if isinstance(definition, StatefulDefinition):
        definition.verify_sources()
        if conditions is not None:
            raise ResourcePackError('Workflow v3 uses declared routers, not external conditions')
        return StatefulWorkflow(definition, catalog=registry, reducers=reducers or {},
                                services=services, journal=journal, checkpointer=checkpointer,
                                config=config)
    definition.verify_native_sources()
    return Workflow(definition, registry, conditions,
                    config=config, services=services)
