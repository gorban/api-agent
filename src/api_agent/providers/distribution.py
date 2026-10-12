"""Read inert provider dependency declarations before loading provider code."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import os
import stat
import tomllib


PROVIDER_SELECTORS = {
    'upstream': 'UPSTREAM_API_PROVIDER', 'api': 'API_PROVIDER',
    'llm': 'LLM_PROVIDER', 'access': 'ACCESS_PROVIDER', 'ui': 'UI_PROVIDER',
}


@dataclass(frozen=True)
class ProviderDependencyManifest:
    root: Path
    owner: str
    dependencies: tuple[str, ...]
    groups: Mapping[str, tuple[str, ...]]
    selected_groups: tuple[str, ...] = ()

    def requirements(self, groups=None):
        groups = self.selected_groups if groups is None else tuple(groups)
        unknown = set(groups) - self.groups.keys()
        if unknown:
            raise ValueError(f'Unknown provider dependency group(s): {", ".join(sorted(unknown))}')
        return tuple(dict.fromkeys((*self.dependencies,
                                    *(item for group in groups for item in self.groups[group]))))

    def select_groups(self, *groups):
        """Freeze one operation's optional groups into its selected descriptor."""
        self.requirements(groups)
        return ProviderDependencyManifest(self.root, self.owner, self.dependencies,
                                          self.groups, tuple(groups))


def read_provider_manifest(root: Path, *, owner: str) -> ProviderDependencyManifest:
    """Read a distribution-owned manifest without importing or installing its code.

    The manifest travels with a relocated provider package. Missing manifests
    mean no provider-specific Python requirements.
    """
    root = Path(root).resolve()
    if not root.is_dir() or not isinstance(owner, str) or not owner.strip():
        raise ValueError('Provider manifest needs an existing root and owner')
    path = root / 'provider-dependencies.toml'
    if not path.resolve().is_relative_to(root):
        raise ValueError('Provider dependency manifest must stay inside its root')
    try:
        before = path.lstat()
    except FileNotFoundError:
        return ProviderDependencyManifest(root, owner, (), MappingProxyType({}))
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Provider dependency manifest must be a regular file')
    try:
        # O_NONBLOCK prevents a swapped FIFO from stalling POSIX startup. On
        # platforms lacking it, the lstat/fstat comparison catches replacement
        # after open; native replacement-race protection remains unverified.
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('Provider dependency manifest must be a regular file')
            data = tomllib.load(stream)
    except OSError as exc:
        raise ValueError('Provider dependency manifest cannot be read safely') from exc
    if set(data) - {'version', 'dependencies', 'groups'} or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('Provider dependency manifest requires version = 1')
    dependencies = data.get('dependencies', [])
    groups = data.get('groups', {})
    if (not isinstance(dependencies, list) or any(type(item) is not str or not item.strip() for item in dependencies)
            or not isinstance(groups, dict) or any(type(name) is not str or not name.strip()
                or not isinstance(items, list) or any(type(item) is not str or not item.strip() for item in items)
                for name, items in groups.items())):
        raise ValueError('Provider dependencies and groups must be nonempty requirement strings')
    return ProviderDependencyManifest(root, owner, tuple(dependencies),
                                      MappingProxyType({name: tuple(items) for name, items in groups.items()}))


def selected_manifests(values, *, owner_root: Path, active_kinds, groups_by_kind=None):
    """Resolve only execution-active provider locations to inert manifests."""
    from api_agent.providers.loading import provider_location

    unknown = set(active_kinds) - PROVIDER_SELECTORS.keys()
    if unknown:
        raise ValueError(f'Unknown provider kind(s): {", ".join(sorted(unknown))}')
    selected = {}
    groups_by_kind = groups_by_kind or {}
    for kind, field in PROVIDER_SELECTORS.items():
        if kind not in active_kinds or not values.get(field):
            continue
        path, _ = provider_location(values[field], owner_root=owner_root)
        manifest = selected.get(path.parent)
        if manifest is None:
            manifest = read_provider_manifest(path.parent, owner=str(path.parent))
        groups = tuple(dict.fromkeys((*manifest.selected_groups, *groups_by_kind.get(kind, ()))))
        selected[path.parent] = manifest.select_groups(*groups)
    return tuple(selected.values())


def inspect_relocated_pack(base_root: Path, pack_root: Path):
    """Validate a complete installed pack's inert declarations and dependencies.

    Return the context and composed schema without importing factory code.
    """
    from api_agent.config.declarations import compose_schema, load_declarations, load_shared_schema
    from api_agent.runtime.resource_pack import load_launch_context

    context = load_launch_context(base_root, pack_root)
    shared = load_shared_schema(base_root)
    schema = compose_schema(shared,
                            load_declarations(context.config_path, base_schema=shared,
                                              source='pack_default'), context.defaults)
    for path in context.provider_config_paths:
        schema = compose_schema(schema,
                                load_declarations(path, base_schema=schema,
                                                  source='pack_default'))
    # Check file-backed factory defaults after relocation; no module is loaded.
    from api_agent.config.declarations import UNSET
    from api_agent.providers.loading import provider_location
    for selector in PROVIDER_SELECTORS.values():
        declaration = schema.fields.get(selector)
        if declaration is None or declaration.default is UNSET or not declaration.default:
            continue
        owner = context.owner_root if declaration.default_source == 'pack_default' else context.base_root
        path, _ = provider_location(declaration.default, owner_root=owner)
        read_provider_manifest(path.parent, owner=str(path.parent))
    return context, schema
