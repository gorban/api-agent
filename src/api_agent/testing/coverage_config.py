"""Owner declarations may opt into child-interpreter coverage, without changing gates."""

from pathlib import Path
import tomllib

from api_agent.runtime.pack_admission import declaration_bodies, verify_composition
from api_agent.runtime.resource_pack import ResourcePackError


def _declared_patches(body):
    document = tomllib.loads(body.decode())
    for name in ('tool', 'coverage', 'run'):
        document = document.get(name, {})
        if not isinstance(document, dict):
            raise ValueError('Unit coverage settings require TOML tables')
    if 'patch' not in document:
        return None
    patches = document['patch']
    if not isinstance(patches, list) or any(patch != 'subprocess' for patch in patches):
        raise ValueError('Unit coverage patch must be a list containing only subprocess')
    return tuple(dict.fromkeys(patches))


def owner_coverage_patches(context, root):
    """Resolve captured owner policy, coalescing compatible explicit pack policies.

    A captured exact owner-root project wins, even without an explicit patch.
    Otherwise packs mapped to this owner may
    supply a policy: absent declarations abstain, identical explicit policies
    coalesce, and conflicting explicit policies are rejected.
    """
    verify_composition(context)
    contributions = (*getattr(context, 'pack_contributions', ()),
                     *getattr(context, 'project_contributions', ()))
    root = Path(root)
    if contributions:
        bodies = declaration_bodies(context)
        exact = [item for item in contributions if item.pack_root == root
                 and item.pack_root / 'pyproject.toml' in bodies]
        matching = exact or [item for item in contributions if item.owner_root == root]
        if not matching:
            raise ResourcePackError('Unit coverage owner requires an admitted declaration')
        policies = {_declared_patches(bodies.get(item.pack_root / 'pyproject.toml', b''))
                    for item in matching}
        policies.discard(None)
        if len(policies) > 1:
            raise ResourcePackError('Unit coverage owner has conflicting explicit patch policies')
        return next(iter(policies), ())
    manifest = root / 'pyproject.toml'
    body = manifest.read_bytes() if manifest.is_file() else b''
    return _declared_patches(body) or ()
