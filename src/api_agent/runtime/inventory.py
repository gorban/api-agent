"""Declaration-only authority for ignoring unselected neighbors in one scope."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import tomllib
from types import MappingProxyType

from .infrastructure_contract import (_capture, _relative, _identifier,
    MAX_DECLARATIONS, MAX_SERVICES, MAX_DECLARATION_BYTES)
from .ownership import StackScope


def inventory_authority(context, plan=None):
    """Use explicit normal declarations and already admitted tool descriptors only."""
    owners, source_owners = {}, {}
    def add(owner, root, paths):
        root = Path(root).absolute()
        if root.resolve(strict=True) != root:
            raise ValueError('Inventory owner root is not canonical')
        new_paths = []
        for path in paths:
            absolute = Path(path).absolute()
            known = source_owners.get(absolute)
            if known is not None:
                if known != owner:
                    raise ValueError('Inventory declaration has conflicting owners')
                continue
            source_owners[absolute] = owner
            new_paths.append(absolute)
        if not new_paths:
            return
        previous = owners.setdefault(owner, (root, set()))
        if previous[0] != root:
            raise ValueError('Inventory owner has conflicting roots')
        for path in new_paths:
            relative = Path(path).relative_to(root).as_posix()
            _relative(relative, 'inventory declaration')
            if not relative.endswith('.toml'):
                raise ValueError('Inventory declaration must be TOML')
            previous[1].add(relative)
    for tool in getattr(plan, 'tools', ()):
        if tool.infrastructure:
            item = tool.infrastructure
            add(item['owner'], item['owner_root'], item['declarations'])
    if getattr(context, 'infrastructure_paths', ()):
        add(context.pack_id, context.pack_root, context.infrastructure_paths)
    for owner, root, paths in getattr(context, 'inventory_sources', ()):
        add(owner, root, paths)
    result = []
    for owner, (root, paths) in sorted(owners.items()):
        _identifier(owner, 'inventory owner')
        result.extend({'owner': owner, 'root': str(root), 'path': path} for path in sorted(paths))
    if len(result) > MAX_DECLARATIONS:
        raise ValueError('Inventory declaration count exceeds budget')
    return result


def _pairs(authority, declarations):
    from .infrastructure_plan import _name
    if (type(authority) is not list or type(declarations) is not list
            or len(authority) != len(declarations) or len(authority) > MAX_DECLARATIONS):
        raise ValueError('Invalid inventory declaration authority')
    owners, identities, names = {}, set(), {}
    pairs, profile_owners = [], {}
    for row, captured in zip(authority, declarations, strict=True):
        if (type(row) is not dict or set(row) != {'owner', 'root', 'path'}
                or any(type(row[key]) is not str for key in row)
                or type(captured) is not dict or set(captured) != {'body', 'sha256'}
                or type(captured['body']) is not str or type(captured['sha256']) is not str):
            raise ValueError('Invalid inventory provenance')
        owner = _identifier(row['owner'], 'inventory owner')
        root = Path(row['root'])
        if not root.is_absolute() or str(root) != row['root'] or '..' in root.parts:
            raise ValueError('Invalid inventory owner root')
        if owners.setdefault(owner, root) != root:
            raise ValueError('Inventory owner has conflicting roots')
        _relative(row['path'], 'inventory declaration')
        if not row['path'].endswith('.toml'):
            raise ValueError('Inventory declaration must be TOML')
        body = captured['body'].encode('utf-8')
        if (len(body) > MAX_DECLARATION_BYTES or sha256(body).hexdigest() != captured['sha256']):
            raise ValueError('Invalid inventory declaration receipt')
        data = tomllib.loads(body.decode('utf-8'))
        if (set(data) != {'version', 'owner', 'services'} or type(data['version']) is not int
                or data['version'] != 1 or data['owner'] != owner or type(data['services']) is not list):
            raise ValueError('Invalid inventory declaration envelope')
        for service in data['services']:
            if type(service) is not dict:
                raise ValueError('Invalid inventory service identity')
            local = _identifier(service.get('id'), 'inventory service')
            identity = owner + '/' + local
            if identity in identities:
                raise ValueError('Duplicate inventory service identity')
            identities.add(identity)
            if len(identities) > MAX_SERVICES:
                raise ValueError('Inventory service count exceeds budget')
            name, profile = _name('infra-', identity), _name('infra-', owner)
            if (name in names or profile_owners.setdefault(profile, owner) != owner):
                raise ValueError('Inventory generated name collision')
            names[name] = identity
            pairs.append((name, profile))
    return tuple(sorted(pairs))


@dataclass(frozen=True)
class InventoryCatalog:
    scope_id: str
    authority: tuple
    declarations: tuple
    pairs: tuple

    def __post_init__(self):
        object.__setattr__(self, 'authority', tuple(MappingProxyType(dict(row)) for row in self.authority))
        object.__setattr__(self, 'declarations', tuple(MappingProxyType(dict(row)) for row in self.declarations))
        object.__setattr__(self, 'pairs', tuple(tuple(pair) for pair in self.pairs))

    def to_wire(self):
        return {'scope': self.scope_id, 'authority': [dict(row) for row in self.authority],
                'declarations': [dict(row) for row in self.declarations]}

    def verify(self, scope):
        value = self.to_wire()
        restored = type(self).from_wire(value, scope=scope, authority=value['authority'])
        if restored.pairs != self.pairs:
            raise ValueError('Inventory pairs differ from captured declarations')
        return restored.pairs

    def verify_plan(self, plan):
        sources = {(Path(row['root']) / row['path']): (row['owner'], captured['body'].encode())
                   for row, captured in zip(self.authority, self.declarations, strict=True)}
        for tool in plan.tools:
            if not tool.infrastructure:
                continue
            for path in tool.infrastructure['declarations']:
                expected = sources.get(Path(path))
                if expected is None or expected[0] != tool.infrastructure['owner']:
                    raise ValueError('Inventory excludes an admitted tool owner')
        for path, body in getattr(plan, 'captured_assets', {}).items():
            if path in sources and sources[path][1] != body:
                raise ValueError('Inventory conflicts with admitted tool declaration bytes')

    @classmethod
    def from_wire(cls, value, *, scope, authority):
        if (type(value) is not dict or set(value) != {'scope', 'authority', 'declarations'}
                or value['scope'] != scope.id or value['authority'] != authority):
            raise ValueError('Inventory differs from trusted launch authority')
        pairs = _pairs(authority, value['declarations'])
        return cls(scope.id, tuple(authority), tuple(value['declarations']), pairs)


def capture_inventory(context, plan=None):
    authority = inventory_authority(context, plan)
    rows = []
    existing = getattr(plan, 'captured_assets', {})
    for row in authority:
        path = Path(row['root']) / row['path']
        body = existing.get(path)
        if body is None:
            body = _capture(Path(row['root']), row['path'], MAX_DECLARATION_BYTES).body
        rows.append({'body': body.decode('utf-8'), 'sha256': sha256(body).hexdigest()})
    scope = StackScope.from_roots(context.base_root, context.pack_root)
    catalog = InventoryCatalog.from_wire({'scope': scope.id, 'authority': authority,
        'declarations': rows}, scope=scope, authority=authority)
    if plan is not None:
        catalog.verify_plan(plan)
    return catalog
