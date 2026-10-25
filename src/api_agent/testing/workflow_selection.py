"""Static exact-case workflow associations, independent of pytest imports."""
from dataclasses import dataclass
from pathlib import Path
import re

from api_agent.tooling.models import ToolSelectionError, freeze

MAX_GRAPHS = 256
MAX_CASES = 8192
_KEY = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')


@dataclass(frozen=True)
class WorkflowCatalogDeclaration:
    graphs: object
    test_cases: object
    variants: object
    profiles: object

    def __post_init__(self):
        for name in ('graphs', 'test_cases', 'variants', 'profiles'):
            object.__setattr__(self, name, freeze(getattr(self, name)))


def _graph(value, roots):
    if (type(value) is not dict or not {'root', 'entry'} <= set(value)
            or set(value) - {'root', 'entry', 'replacements', 'profile', 'resource_root'}):
        raise ToolSelectionError('Catalog graph requires root and entry')
    if type(value['root']) is not str or value['root'] not in roots:
        raise ToolSelectionError('Catalog graph has unknown owning root')
    _relative(value['entry'], '.md.html')
    if 'resource_root' in value:
        _relative(value['resource_root'], '')
    replacements = value.get('replacements', [])
    if type(replacements) is not list or len(replacements) > MAX_GRAPHS:
        raise ToolSelectionError('Catalog replacements require a bounded array')
    targets = set()
    for row in replacements:
        if (type(row) is not dict or set(row) != {'target_id', 'entry', 'root_id'}
                or type(row['target_id']) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', row['target_id'])
                or row['target_id'] in targets or type(row['root_id']) is not str or row['root_id'] not in roots):
            raise ToolSelectionError('Invalid catalog replacement')
        targets.add(row['target_id'])
        _relative(row['entry'], '.md.html')
    return value


def _relative(value, suffix):
    if (type(value) is not str or not value or len(value) > 1024
            or Path(value).is_absolute() or '..' in Path(value).parts
            or '\\' in value or Path(value).as_posix() != value or not value.endswith(suffix)):
        raise ToolSelectionError('Catalog paths must be owner-relative declarations')


def parse_catalog(data, roots):
    if (type(data) is not dict or type(data.get('version')) is not int or data['version'] != 1
            or set(data) - {'version', 'graphs', 'test_cases', 'variants', 'profiles'}):
        raise ToolSelectionError('Tool catalog requires version 1')
    graphs, cases, variants, profiles = (data.get(name, {}) for name in ('graphs', 'test_cases', 'variants', 'profiles'))
    if any(type(item) is not dict for item in (graphs, cases, variants, profiles)):
        raise ToolSelectionError('Tool catalog sections must be tables')
    if len(graphs) > MAX_GRAPHS or len(variants) > MAX_GRAPHS or len(cases) > MAX_CASES or len(profiles) > MAX_GRAPHS:
        raise ToolSelectionError('Tool catalog exceeds its finite identity budget')
    for name, spec in profiles.items():
        if (not _KEY.fullmatch(name) or type(spec) is not dict
                or set(spec) != {'version', 'kind', 'primary', 'secondary'}
                or type(spec['version']) is not int or spec['version'] != 1
                or spec['kind'] != 'shared-primary'
                or type(spec['primary']) is not str or not spec['primary']
                or type(spec['secondary']) is not list or not spec['secondary']
                or any(type(item) is not str or not item or item == spec['primary'] for item in spec['secondary'])
                or len(set(spec['secondary'])) != len(spec['secondary'])):
            raise ToolSelectionError('Invalid shared-primary catalog profile')
    for name, spec in graphs.items():
        if not _KEY.fullmatch(name):
            raise ToolSelectionError('Invalid catalog graph identity')
        _graph(spec, roots)
        if 'profile' in spec and (type(spec['profile']) is not str or spec['profile'] not in profiles):
            raise ToolSelectionError('Unknown graph profile')
    for name, spec in variants.items():
        if (not _KEY.fullmatch(name) or name in graphs or type(spec) is not dict
                or not {'graph', 'replacements'} <= set(spec)
                or set(spec) - {'graph', 'replacements', 'profile'}
                or type(spec['graph']) is not str or spec['graph'] not in graphs):
            raise ToolSelectionError('Invalid finite catalog variant')
        graph = dict(graphs[spec['graph']], replacements=spec['replacements'])
        if 'profile' in spec:
            graph['profile'] = spec['profile']
        _graph(graph, roots)
        if graph.get('profile') is not None and (type(graph['profile']) is not str or graph['profile'] not in profiles):
            raise ToolSelectionError('Unknown variant profile')
    normalized = {}
    for identity, spec in cases.items():
        if (type(identity) is not str or len(identity) > 2048 or '::' not in identity
                or any(ord(char) < 32 for char in identity)
                or identity.startswith(('::', 'tag:')) or len(identity.split('::', 2)) != 3
                or not identity.split('::', 2)[1].endswith('.py') or not identity.split('::', 2)[2]):
            raise ToolSelectionError('Catalog cases require exact repository-qualified pytest identities')
        if isinstance(spec, list):
            spec = {'graphs': spec}
        if (type(spec) is not dict or 'graphs' not in spec or set(spec) - {'graphs', 'profile'}
                or type(spec['graphs']) is not list or len(spec['graphs']) > MAX_GRAPHS
                or any(type(item) is not str or item not in graphs and item not in variants for item in spec['graphs'])
                or len(set(spec['graphs'])) != len(spec['graphs'])
                or 'profile' in spec and (type(spec['profile']) is not str or spec['profile'] not in profiles)):
            raise ToolSelectionError('Invalid exact-case graph association')
        normalized[identity] = spec
    return WorkflowCatalogDeclaration(graphs, normalized, variants, profiles)


def selected_graphs(declaration, selection):
    """Graphless unmapped tests contribute no application graph or tool inputs."""
    graphs, profiles = [], []
    if selection.mode == 'unit':
        return (), ()
    for identity in selection.test_ids:
        spec = declaration.test_cases.get(identity)
        if spec is None:
            if identity not in selection.graphless and selection.mode != 'unit':
                raise ToolSelectionError('Selected exact test identity has no static graph association')
            spec = {'graphs': ()}
        graphs.extend(spec['graphs'])
        if 'profile' in spec:
            profiles.append(spec['profile'])
    for variant in selection.variants:
        if variant not in declaration.variants:
            raise ToolSelectionError('Unknown trusted workflow variant')
        graphs.append(variant)
    return tuple(dict.fromkeys(graphs)), tuple(dict.fromkeys(profiles))
