"""Pure, immutable projection of one admitted selected workflow for static viewing."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from .definition import InlinePromptRef
from .document_format import canonical_executable_bytes


@dataclass(frozen=True)
class ViewerDocument:
    logical_path: str
    stable_id: str
    owner_id: str | None
    flow_line: int
    raw_bytes: bytes
    executable_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class ViewerStep:
    id: str
    kind: str
    parent: str | None
    owner_path: str
    flow_line: int
    declaration_path: str
    declaration_fragment: str = ''


@dataclass(frozen=True)
class ViewerEdge:
    owner: str
    source: str
    target: str
    condition: str
    implicit: bool


@dataclass(frozen=True)
class ViewerReference:
    kind: str
    owner_path: str
    owner_id: str | None
    line: int
    target_path: str
    target_sha256: str
    attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ViewerController:
    component_id: str
    guard: str
    max_passes: int
    accepted: bool
    retry: bool
    exhaustion: bool


@dataclass(frozen=True)
class ViewerProjection:
    version: int
    graph_id: str
    definition_id: str
    entry_path: str
    documents: tuple[ViewerDocument, ...]
    steps: tuple[ViewerStep, ...]
    edges: tuple[ViewerEdge, ...]
    backlinks: tuple[ViewerEdge, ...]
    references: tuple[ViewerReference, ...]
    resources: tuple
    replacements: tuple
    projection_sha256: str
    controllers: tuple[ViewerController, ...] = ()


def project_view(definition) -> ViewerProjection:
    """Use admitted bytes only; do not refresh, import, or execute the graph."""
    if definition is None or definition.selected_declarations is None:
        raise ValueError('an admitted selected workflow definition is required')
    roots = definition.roots
    resource_by_path = {item.logical_path: item for item in definition.resources}

    def logical(path):
        if isinstance(path, InlinePromptRef):
            owner_root = logical(path.owner).split('/', 1)[0]
            return f'inline/{owner_root}/{path.id}'
        path = Path(path)
        if path == definition.entry:
            key = definition.entry_root
        else:
            owners = [(key, root) for key, root in roots.items() if path.is_relative_to(root)]
            if not owners:
                raise ValueError('projection path outside admitted roots')
            admitted = [(key, root) for key, root in owners
                        if key + '/' + path.relative_to(root).as_posix() in resource_by_path]
            key, _ = max(admitted or owners,
                         key=lambda item: (len(item[1].parts), item[0] == definition.entry_root))
        return key + '/' + path.relative_to(roots[key]).as_posix()

    declarations = [(definition.entry, definition.graph_id, None, 1)]
    declarations.extend((target, identifier, next((step.parent for step in definition.layout.steps
                                                    if step.id == identifier), None), line)
                        for identifier, (_, line, target) in definition.selected_declarations.items())
    documents = []
    seen_documents = set()
    for path, identifier, parent, line in declarations:
        if path in seen_documents:
            continue
        seen_documents.add(path)
        raw = definition.contents[path]
        canonical = canonical_executable_bytes(raw, path=path)
        digest = sha256(canonical).hexdigest()
        documents.append(ViewerDocument(logical(path), identifier, parent, line, raw, canonical, digest))
    steps = tuple(ViewerStep(step.id, step.kind, step.parent,
                             logical(definition.selected_declarations[step.id][0]),
                             definition.selected_declarations[step.id][1],
                             logical(definition.selected_declarations[step.id][2]),
                             (getattr(definition, 'declaration_fragments', None) or {}).get(step.id, ''))
                  for step in definition.layout.steps)

    if hasattr(definition, 'nodes'):
        edges = [ViewerEdge('GRAPH', 'START', steps[0].id, 'always', True)]
        for component in (step for step in steps if step.kind == 'component'):
            first_child = next(step for step in steps if step.parent == component.id)
            edges.append(ViewerEdge(component.id, 'START', first_child.id, 'always', True))
        edges.extend(ViewerEdge(next((s.parent for s in steps if s.id == row['From']), None) or 'GRAPH',
                                row['From'], row['To'], 'always' if row['Router'] == '-' else row['Router'],
                                False) for row in definition.edges)
    else:
        edges = []
        for parent in (None, *(step.id for step in steps if step.kind == 'component')):
            children = [step for step in steps if step.parent == parent]
            owner = parent or 'GRAPH'
            edges.append(ViewerEdge(owner, 'START', children[0].id, 'always', True))
            for index, step in enumerate(children):
                declared = [row for row in definition.layout.tables['Edges'] if row['From'] == step.id]
                if declared:
                    edges.extend(ViewerEdge(owner, step.id, row['To'], row['When'], False)
                                 for row in declared)
                else:
                    target = children[index + 1].id if index + 1 < len(children) else 'END'
                    edges.append(ViewerEdge(owner, step.id, target, 'always', True))
    references = []

    def add_reference(kind, owner, owner_id, line, target, **attrs):
        target_path = logical(target)
        resource = resource_by_path.get(target_path)
        if resource is None:
            raise ValueError(f'missing captured viewer reference: {target_path}')
        references.append(ViewerReference(kind, logical(owner), owner_id, line, target_path,
                                          resource.sha256, tuple(sorted((str(k), str(v)) for k, v in attrs.items()))))

    if hasattr(definition, 'nodes'):
        add_reference('state_schema', definition.entry, None, 1, definition.schema_source,
                      registry=definition.schema_name)
        for row in definition.nodes:
            add_reference('native_source', definition.selected_declarations[row['ID']][2], row['ID'], 1,
                          row['Source'], registry=row['Registry'], role=row['Kind'])
        routed = {}
        for row in definition.edges:
            if row['Source'] is not None:
                routed.setdefault((row['From'], row['Router'], row['Source']), []).append(row)
        for (owner_id, router, source), routes in routed.items():
            add_reference('router_source', definition.entry, owner_id, routes[0]['line'], source,
                          router=router, targets=','.join(row['To'] for row in routes))
        for row in definition.timeouts:
            add_reference('timeout', definition.entry, row['Owner'], row['line'], row['Source'], key=row['Key'])
        for identifier, resource in definition.declared_resources.items():
            references.append(ViewerReference(resource.kind, logical(definition.entry), identifier, 1,
                                              resource.logical_path, resource.sha256, ()))
    else:
        owner_by_path = {target: identifier for identifier, (_, _, target) in definition.selected_declarations.items()}
        for name, fields in (('Functions', ('Input', 'Output')),
                             ('Prompts', ('File',)), ('Timeouts', ('Source',))):
            for row in definition.layout.tables[name]:
                owner = row.get('_owner', definition.entry)
                for field in fields:
                    target = row[field]
                    attrs = {key: value for key, value in row.items()
                             if key not in ('line', '_owner', field) and isinstance(value, str)}
                    if name == 'Functions':
                        owner_id = row['ID']
                    elif name == 'Timeouts':
                        owner_id = None if row['Owner'] == 'GRAPH' else row['Owner']
                    else:
                        owner_id = row.get('_owner_id', owner_by_path.get(owner))
                    add_reference(name.lower(), owner, owner_id, row['line'], target,
                                  field=field, **attrs)
        for row in definition.layout.tables['Bindings']:
            owner = row.get('_owner', definition.entry)
            references.append(ViewerReference('binding', logical(owner), row.get('_owner_id', owner_by_path.get(owner)), row['line'],
                                              '', '', tuple(sorted((key, str(value)) for key, value in row.items()
                                                                   if key not in ('line', '_owner')))))
    controllers = ()
    if not hasattr(definition, 'nodes'):
        from .predicates import component_controller
        controllers = tuple(ViewerController(row['ID'], **component_controller(row))
                            for row in definition.layout.tables['Components'])
    payload = {'version': 1, 'graph_id': definition.graph_id,
               'entry_path': logical(definition.entry),
               'documents': [(d.logical_path, d.stable_id, d.owner_id, d.sha256) for d in documents],
               'steps': [(s.id, s.kind, s.parent, s.owner_path, s.declaration_path,
                          s.declaration_fragment) for s in steps],
               'edges': [(e.owner, e.source, e.target, e.condition, e.implicit) for e in edges],
               'references': [(r.kind, r.owner_path, r.owner_id, r.target_path, r.target_sha256,
                               r.attributes) for r in references],
               'resources': [(r.kind, r.logical_path,
                              next((d.sha256 for d in documents if d.logical_path == r.logical_path),
                                   r.sha256)) for r in definition.resources],
               'replacements': [(r.target_id, r.root_id, Path(r.entry).as_posix()) for r in definition.replacements]}
    payload['controllers'] = [(c.component_id, c.guard, c.max_passes, c.accepted,
                               c.retry, c.exhaustion) for c in controllers]
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    backlinks = tuple(sorted(edges, key=lambda edge: (edge.owner, edge.target, edge.source,
                                                       edge.condition, edge.implicit)))
    return ViewerProjection(1, definition.graph_id, definition.definition_id, logical(definition.entry),
                            tuple(documents), steps, tuple(edges), backlinks, tuple(references),
                            definition.resources, definition.replacements, digest, controllers)
