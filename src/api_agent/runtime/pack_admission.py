"""Inert, immutable declaration identities for ordered resource packs."""
from dataclasses import dataclass
from hashlib import sha256
import json
import tomllib
from pathlib import Path

from .infrastructure_contract import CapturedAsset, _capture
from .resource_pack import ResourcePackError

MAX_DECLARATION_BYTES = 1024 * 1024
MAX_DECLARATIONS = 64


@dataclass(frozen=True)
class PackContribution:
    pack_id: str
    pack_root: Path
    owner_root: Path
    declarations: tuple[CapturedAsset, ...]
    absent_declarations: tuple[str, ...] = ()
    root_identity: tuple[int, int] = ()
    owner_identity: tuple[int, int] = ()

    def verify(self):
        if (self.pack_root.resolve() != self.pack_root or self.owner_root.resolve() != self.owner_root
                or physical_identity(self.pack_root) != self.root_identity
                or physical_identity(self.owner_root) != self.owner_identity):
            raise ResourcePackError('Selected pack ownership changed after admission')
        for relative in self.absent_declarations:
            path = self.pack_root / relative
            if path.exists() or path.is_symlink():
                raise ResourcePackError('Selected pack declaration appeared after admission')
        for declaration in self.declarations:
            if capture(self.pack_root, declaration.relative_path) != declaration:
                raise ResourcePackError('Selected pack declaration changed after admission')


def physical_identity(path):
    try:
        info = path.stat()
    except OSError as error:
        raise ResourcePackError('Selected pack owner directory is unavailable') from error
    return info.st_dev, info.st_ino


def capture(root, relative):
    try:
        return _capture(root, relative, MAX_DECLARATION_BYTES)
    except (OSError, ValueError) as error:
        raise ResourcePackError('Pack declarations require bounded regular files without symlinks') from error


def admit(layer, manifest):
    manifest_data = tomllib.loads(manifest.body.decode())
    paths = (manifest_data['config'], *manifest_data.get('providers', {}).get('configs', []))
    if len(paths) > MAX_DECLARATIONS:
        raise ResourcePackError('Ordered pack has too many configuration declarations')
    declarations = [manifest]
    for relative in dict.fromkeys(paths):
        declarations.append(capture(layer.pack_root, relative))
    project = layer.pack_root / 'pyproject.toml'
    absent = ()
    if project.exists() or project.is_symlink():
        declarations.append(capture(layer.pack_root, 'pyproject.toml'))
    else:
        absent = ('pyproject.toml',)
    contribution = PackContribution(layer.pack_id, layer.pack_root, layer.owner_root, tuple(declarations), absent,
                                    physical_identity(layer.pack_root), physical_identity(layer.owner_root))
    contribution.verify()
    return contribution


def composition_report(context):
    """Return identities only; configuration and dotenv values are never included."""
    return {'version': 1, 'scope': 'selected-pack-and-project-declarations', 'base_root': str(context.base_root),
            'launcher_root': str(context.launcher_root or context.base_root),
            'contributions': [
                {'id': item.pack_id, 'root': str(item.pack_root), 'owner': str(item.owner_root),
                 'root_identity': item.root_identity, 'owner_identity': item.owner_identity,
                 'absent_declarations': list(item.absent_declarations),
                 'declarations': [{'path': source.relative_path, 'sha256': source.sha256}
                                  for source in item.declarations]}
                for item in (*getattr(context, 'pack_contributions', ()),
                             *getattr(context, 'project_contributions', ()))]}


def composition_identity(context):
    return sha256(json.dumps(composition_report(context), sort_keys=True).encode()).hexdigest()


def verify_composition(context, expected=None):
    for contribution in (*getattr(context, 'pack_contributions', ()),
                         *getattr(context, 'project_contributions', ())):
        contribution.verify()
    if expected is not None and composition_identity(context) != expected:
        raise ResourcePackError('Ordered pack handoff does not match admitted composition')


def declaration_bodies(context):
    """Captured declaration bytes keyed by their original provenance paths."""
    return {item.pack_root / source.relative_path: source.body
            for item in (*getattr(context, 'pack_contributions', ()),
                         *getattr(context, 'project_contributions', ()))
            for source in item.declarations}


def configuration_inputs(context):
    bodies = declaration_bodies(context)
    return {'configuration_bodies': bodies} if bodies else {}


def admit_project(root, owner, owned_files=None):
    root = Path(root).resolve()
    path = root / 'pyproject.toml'
    declarations = ()
    absent = ('pyproject.toml',)
    if path.exists() or path.is_symlink():
        if owned_files is not None and 'pyproject.toml' not in owned_files:
            raise ResourcePackError('Workflow project declaration is not owned by installed distribution')
        declarations = (capture(root, 'pyproject.toml'),)
        absent = ()
    identity = physical_identity(root)
    return PackContribution(owner, root, root, declarations, absent, identity, identity)
