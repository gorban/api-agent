"""Runtime support for canonical compiled workflow plans.

Plans are generated from declarations at build time.  Runtime loads their
encoded graph without importing the declaration parser or executing workflow
source.  Bound modules use ordinary Python imports; their recorded hashes are
provenance for traces and static freshness diagnostics, not runtime guards.
"""
import base64
import csv
from dataclasses import dataclass
from email.parser import BytesHeaderParser
import hashlib
import hmac
import importlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import ClassVar

from .model import InlinePromptRef, Layout, LayoutError, Replacement, Resource, Step, ToolUsage

PLAN_FORMAT = 'api-agent-workflow-plan'
PLAN_VERSION = 1
RUNTIME_ABI = 1
DIGEST_PREFIX = 'sha256:'
SOURCE_KINDS = frozenset({'declaration', 'native_source', 'native_dependency'})
DATA_KINDS = frozenset({'prompt', 'configuration', 'data', 'schema', 'asset', 'tool_instance'})
MAX_PLAN_BYTES = 64 * 1024 * 1024
ARTIFACT_ROOT = PurePosixPath('/artifact')

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_RECORD_HASH = re.compile(r'sha256=([A-Za-z0-9_-]{43})\Z')
_MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+!-]*\Z')
_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z')
_REGISTRY = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
_GRAPH_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_TOP_KEYS = frozenset({
    'format', 'plan_version', 'runtime_abi', 'workflow_version', 'graph_id', 'state', 'imports', 'declaration_entry',
    'definition_id', 'entry_root', 'entry', 'replacements', 'resources', 'layout',
    'selected_declarations', 'declaration_fragments', 'tool_usages', 'bindings',
    'distributions', 'compiled_plan_identity',
})
_STATE_KEYS = frozenset({
    'helper_sources', 'native_dependencies', 'timeouts', 'schema_name',
    'schema_source', 'nodes', 'edges', 'declared_resources', 'application_binding',
})


class ArtifactRejected(LayoutError):
    """A plan cannot be decoded by this runtime ABI."""


def canonical_json(value):
    """The one plan encoding: sorted keys, no whitespace, ASCII, finite numbers."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def plan_digest(plan):
    """The exact-plan digest retained for provenance, never a runtime trust root."""
    return DIGEST_PREFIX + hashlib.sha256(plan).hexdigest()


def compiled_plan_identity(value):
    """Hash the encoded graph fields excluding its self-describing identity."""
    if type(value) is not dict:
        raise ValueError('compiled plan identity requires an object')
    return hashlib.sha256(canonical_json({
        key: item for key, item in value.items() if key != 'compiled_plan_identity'
    })).hexdigest()


def compiled_provenance(definition):
    """Trace-ready plan, source and module identities without reading source files."""
    sources = [
        {'path': item.logical_path, 'sha256': item.sha256, 'size': item.size}
        for item in definition.resources if item.kind in SOURCE_KINDS
    ]
    modules = [
        {'module': row['module'], 'source': row['source'], 'sha256': row['sha256'],
         'distribution': row['distribution']}
        for row in definition.bindings
    ]
    manifest = {
        'version': 1,
        'kind': 'compiled_plan',
        'plan_digest': definition.plan_digest,
        'compiled_plan_identity': definition.compiled_plan_identity,
        'source_hashes': sorted(sources, key=lambda row: row['path']),
        'module_hashes': sorted(modules, key=lambda row: row['module']),
    }
    manifest['sha256'] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def module_for_file(file):
    """The dotted module a distribution-relative ``.py`` path provides."""
    parts = file.split('/')
    if not parts[-1].endswith('.py'):
        return None
    stem = parts[-1][:-3]
    names = parts[:-1] if stem == '__init__' else [*parts[:-1], stem]
    if not names or not all(_MODULE.fullmatch(name) and '.' not in name for name in names):
        return None
    return '.'.join(names)


def _safe_relative(path):
    parts = path.split('/')
    if (not path or path.startswith('/') or '\\' in path or '\x00' in path
            or any(part in ('', '.', '..') for part in parts)):
        raise ArtifactRejected(f'unsafe distribution path: {path!r}')
    return path


# These three helpers are build-time wheel validation support.  They are never
# called by ``load_artifact``.
def parse_record(record, dist_info):
    """Parse a wheel RECORD into ``{path: (sha256 hex, size)}``."""
    try:
        rows = list(csv.reader(io.StringIO(record.decode('utf-8'), newline='')))
    except (UnicodeError, csv.Error) as error:
        raise ArtifactRejected('RECORD is not valid CSV text') from error
    entries = {}
    for row in rows:
        if len(row) != 3:
            raise ArtifactRejected('RECORD rows require path, hash and size')
        path = _safe_relative(row[0])
        if path in entries:
            raise ArtifactRejected(f'duplicate RECORD entry: {path}')
        if path == f'{dist_info}/RECORD':
            if row[1] or row[2]:
                raise ArtifactRejected('RECORD must not describe its own hash')
            entries[path] = (None, None)
            continue
        match = _RECORD_HASH.fullmatch(row[1])
        if match is None or not row[2].isdigit():
            raise ArtifactRejected(f'RECORD entry requires sha256 and size: {path}')
        entries[path] = (base64.urlsafe_b64decode(match.group(1) + '=').hex(), int(row[2]))
    if f'{dist_info}/RECORD' not in entries:
        raise ArtifactRejected('RECORD does not list itself')
    return entries


def verify_record(entries, files):
    """Verify a source wheel's RECORD at compilation time."""
    missing, unexpected = set(entries) - set(files), set(files) - set(entries)
    if missing or unexpected:
        raise ArtifactRejected(f'wheel differs from RECORD: missing {sorted(missing)}, '
                               f'unexpected {sorted(unexpected)}')
    verified = {}
    for path, (digest, size) in entries.items():
        if digest is None:
            continue
        data = files[path]
        if len(data) != size:
            raise ArtifactRejected(f'wheel file size does not match RECORD: {path}')
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ArtifactRejected(f'wheel file hash does not match RECORD: {path}')
        verified[path] = actual
    return verified


def distribution_identity(files, dist_info):
    """Validate source-wheel metadata for compiler provenance."""
    body = files.get(f'{dist_info}/METADATA')
    if body is None:
        raise ArtifactRejected(f'{dist_info}/METADATA is not part of the wheel')
    message = BytesHeaderParser().parsebytes(body)
    name, version = message['Name'], message['Version']
    folder, _, folder_version = dist_info.removesuffix('.dist-info').rpartition('-')
    normalize = lambda value: re.sub(r'[-_.]+', '-', value).lower()
    if (name is None or version is None or not _NAME.fullmatch(name) or not _VERSION.fullmatch(version)
            or normalize(folder) != normalize(name) or folder_version != version):
        raise ArtifactRejected('distribution METADATA does not match its dist-info directory')
    return normalize(name), version


def _virtual(logical):
    if (type(logical) is not str or logical.split('/', 1)[0] not in ('base', 'pack')
            or '/' not in logical or '\\' in logical or '\x00' in logical
            or any(part in ('', '.', '..') for part in logical.split('/'))):
        raise ValueError('invalid logical path')
    return ARTIFACT_ROOT.joinpath(*logical.split('/'))


def _decode(value):
    kind = type(value)
    if value is None or kind in (bool, int, float, str):
        return value
    if kind is list:
        return tuple(_decode(item) for item in value)
    if kind is dict and len(value) == 1:
        (tag, body), = value.items()
        if tag == '$p':
            return _virtual(body)
        if tag == '$i':
            owner, identifier = body
            if type(identifier) is not str:
                raise ValueError('invalid inline prompt reference')
            return InlinePromptRef(_virtual(owner), identifier)
        if tag == '$m':
            result = {}
            for key, item in body:
                if type(key) is not str or key in result:
                    raise ValueError('invalid or duplicate mapping key')
                result[key] = _decode(item)
            return MappingProxyType(result)
    raise ValueError('invalid plan value')


def _strict_pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def _reject_constant(_name):
    raise ValueError('nonfinite JSON number')


def _parse(plan):
    try:
        value = json.loads(plan.decode('ascii'), object_pairs_hook=_strict_pairs,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ArtifactRejected('plan is not strict JSON') from error
    if type(value) is not dict:
        raise ArtifactRejected('plan must be a JSON object')
    try:
        canonical = canonical_json(value)
    except (TypeError, ValueError) as error:
        raise ArtifactRejected('plan is not canonical JSON') from error
    if canonical != plan:
        raise ArtifactRejected('plan is not canonical JSON')
    return value


def _check_versions(value):
    ints = {key: value.get(key) for key in ('plan_version', 'runtime_abi', 'workflow_version')}
    if value.get('format') != PLAN_FORMAT or any(type(item) is not int for item in ints.values()):
        raise ArtifactRejected('not an api-agent workflow plan')
    if ints['plan_version'] != PLAN_VERSION or ints['runtime_abi'] != RUNTIME_ABI:
        raise ArtifactRejected(f"unsupported plan version {ints['plan_version']} "
                               f"or runtime ABI {ints['runtime_abi']}")
    if ints['workflow_version'] != 1:
        raise ArtifactRejected(f"unsupported workflow version {ints['workflow_version']}")
    expected = _TOP_KEYS
    if set(value) != expected:
        raise ArtifactRejected('plan has missing or unknown sections')
    identity = value.get('compiled_plan_identity')
    if type(identity) is not str or not _HEX.fullmatch(identity):
        raise ArtifactRejected('compiled plan identity is invalid')
    if not hmac.compare_digest(identity, compiled_plan_identity(value)):
        raise ArtifactRejected('compiled plan identity does not match the encoded graph')
    return value['state'] is not None


def _definition_id(version, graph_id, resources, replacements):
    return hashlib.sha256(json.dumps({
        'version': version, 'graph_id': graph_id,
        'resources': [(item.kind, item.logical_path, item.sha256) for item in resources],
        'replacements': [(item.target_id, item.root_id, Path(item.entry).as_posix())
                         for item in replacements],
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _decode_resources(rows):
    resources, contents, seen = [], {}, set()
    for row in rows:
        if set(row) != {'kind', 'path', 'sha256', 'size', 'content'}:
            raise ValueError('invalid resource')
        kind, path, digest, size, content = (row[key] for key in ('kind', 'path', 'sha256', 'size', 'content'))
        if (kind not in SOURCE_KINDS | DATA_KINDS or type(path) is not str or path in seen
                or type(digest) is not str or not _HEX.fullmatch(digest)
                or type(size) is not int or size < 0):
            raise ValueError('invalid resource')
        seen.add(path)
        if content is None:
            if kind not in SOURCE_KINDS:
                raise ValueError('data resource requires content')
            resources.append(Resource(kind, path, None, digest, size))
            continue
        if kind in SOURCE_KINDS or set(content) != {'key', 'b64'}:
            raise ValueError('source resource must not carry content')
        data = base64.b64decode(content['b64'], validate=True)
        key = _decode(content['key'])
        named = (f"inline/{_logical(key.owner).split('/', 1)[0]}/{key.id}"
                 if isinstance(key, InlinePromptRef) else _logical(key))
        if named != path:
            raise ValueError('resource key does not name its resource')
        contents[key] = data
        resources.append(Resource(kind, path, data, digest, size))
    return tuple(resources), contents


def _decode_bindings(plan, resources):
    distributions = {}
    for row in plan['distributions']:
        if set(row) != {'name', 'version', 'dist_info', 'files', 'top_levels'}:
            raise ValueError('invalid distribution')
        if (not all(type(row[key]) is str for key in ('name', 'version', 'dist_info'))
                or row['name'] in distributions or not _NAME.fullmatch(row['name'])
                or not _VERSION.fullmatch(row['version']) or type(row['files']) is not list
                or type(row['top_levels']) is not list):
            raise ValueError('invalid distribution')
        distributions[row['name']] = row
    bindings = {}
    for row in plan['bindings']:
        if set(row) != {'source', 'distribution', 'module', 'file', 'sha256'}:
            raise ValueError('invalid binding')
        if (not all(type(row[key]) is str for key in row) or row['source'] in bindings
                or row['distribution'] not in distributions or not _HEX.fullmatch(row['sha256'])
                or not _MODULE.fullmatch(row['module']) or module_for_file(_safe_relative(row['file'])) != row['module']):
            raise ValueError('invalid binding')
        bindings[row['source']] = MappingProxyType(dict(row))
    sources = {item.logical_path for item in resources if item.kind in ('native_source', 'native_dependency')}
    if set(bindings) != sources:
        raise ArtifactRejected('native sources and plan bindings differ')
    return tuple(MappingProxyType(dict(row)) for row in distributions.values()), tuple(bindings.values())


def _logical(path):
    return path.relative_to(ARTIFACT_ROOT).as_posix()


def _row(value, length, name):
    if type(value) is not tuple or len(value) != length:
        raise ValueError(f'invalid {name} row')
    return value


def _decode_plan(value, typed):
    resources, contents = _decode_resources(value['resources'])
    graph_id, entry_root = value['graph_id'], value['entry_root']
    replacements = tuple(Replacement(target, Path(entry), root)
                         for target, root, entry in (
                             _row(_decode(row), 3, 'replacement') for row in value['replacements']))
    if (type(graph_id) is not str or entry_root not in ('base', 'pack')
            or not all(type(item) is str for row in value['replacements'] for item in row)):
        raise ValueError('invalid identity')
    definition_id = _definition_id(1, graph_id, resources, replacements)
    if definition_id != value['definition_id']:
        raise ArtifactRejected('definition_id does not match the plan resources')
    entry = _virtual(value['entry'])
    imports = _decode(value['imports'])
    declaration_entry = _decode(value['declaration_entry'])
    declarations = {_virtual(item.logical_path) for item in resources if item.kind == 'declaration'}
    if (type(imports) is not tuple or len(imports) > 64
            or any(type(row) is not tuple or len(row) != 2
                   or row[0] not in declarations or type(row[1]) is not str
                   or not _GRAPH_ID.fullmatch(row[1]) for row in imports)
            or len({entry, *(row[0] for row in imports)}) != len(imports) + 1
            or declaration_entry != (imports[-1][0] if imports else None)):
        raise ValueError('invalid workflow import metadata')
    encoded_layout = value['layout']
    if set(encoded_layout) != {'steps', 'tables', 'files'} or type(encoded_layout['steps']) is not list:
        raise ValueError('invalid layout')
    steps = []
    for row in encoded_layout['steps']:
        if (type(row) is not dict or set(row) != {'id', 'kind', 'parent', 'source', 'line'}
                or type(row['id']) is not str or row['kind'] not in ('node', 'component')
                or row['parent'] is not None and type(row['parent']) is not str
                or type(row['line']) is not int or row['line'] < 1):
            raise ValueError('invalid layout step')
        steps.append(Step(row['id'], row['kind'], row['parent'], _virtual(row['source']), row['line']))
    tables, files = _decode(encoded_layout['tables']), _decode(encoded_layout['files'])
    expected_tables = {'Components', 'Functions', 'Bindings', 'Edges', 'Prompts', 'Timeouts'} if not typed else {'Edges'}
    if not isinstance(tables, MappingProxyType) or set(tables) != expected_tables:
        raise ValueError('invalid layout tables')
    decoded = dict(
        graph_id=graph_id, entry=entry, entry_root=entry_root,
        imports=imports, declaration_entry=declaration_entry,
        roots=MappingProxyType({root: ARTIFACT_ROOT / root for root in ('base', 'pack')}),
        replacements=replacements, resources=resources, definition_id=definition_id,
        compiled_plan_identity=value['compiled_plan_identity'], layout=Layout(entry, tuple(steps), tables, files),
        contents=MappingProxyType(contents), selected_declarations=_decode(value['selected_declarations']),
        declaration_fragments=_decode(value['declaration_fragments']),
        tool_usages=tuple(ToolUsage(*_row(row, 7, 'tool usage')[:6],
                                    tuple(_row(row, 7, 'tool usage')[6]))
                          for row in _decode(value['tool_usages'])),
    )
    if typed:
        state = value['state']
        if set(state) != _STATE_KEYS:
            raise ValueError('invalid state section')
        nodes, edges, timeouts = (_decode(state[key]) for key in ('nodes', 'edges', 'timeouts'))
        if (not all(isinstance(row, MappingProxyType) and set(row) == {
                'ID', 'Registry', 'Kind', 'Source', '_owner_id', 'line'} for row in nodes)
                or not all(isinstance(row, MappingProxyType) and set(row) == {
                'From', 'To', 'Router', 'Source', 'line'} for row in edges)
                or not all(isinstance(row, MappingProxyType) and set(row) == {
                'Owner', 'Source', 'Key', 'line'} for row in timeouts)):
            raise ValueError('invalid state rows')
        by_logical = {item.logical_path: item for item in resources}
        if state['helper_sources']:
            raise ArtifactRejected('compiled plans do not support helper native sources')
        application_binding = _decode(state['application_binding'])
        native_sources = {_virtual(item.logical_path) for item in resources if item.kind == 'native_source'}
        if application_binding is not None and (
                type(application_binding) is not tuple or len(application_binding) != 2
                or type(application_binding[0]) is not str or not _REGISTRY.fullmatch(application_binding[0])
                or application_binding[1] not in native_sources):
            raise ValueError('invalid application binding')
        decoded.update(
            helper_sources=(), native_dependencies=_decode(state['native_dependencies']),
            timeouts=timeouts, schema_name=state['schema_name'], schema_source=_decode(state['schema_source']),
            nodes=nodes, edges=edges, application_binding=application_binding,
            declared_resources=MappingProxyType({key: by_logical[path]
                                                 for key, path in state['declared_resources'].items()}),
        )
    distributions, bindings = _decode_bindings(value, resources)
    return decoded, distributions, bindings


def _demanded(typed, decoded):
    if not typed:
        steps = {step.id: step for step in decoded['layout'].steps}
        return [(row['ID'], row['Registry'], steps[row['ID']].source)
                for row in decoded['layout'].tables['Functions']]
    return [(None, decoded['schema_name'], decoded['schema_source']),
            *((None, row['Registry'], row['Source']) for row in decoded['nodes']),
            *((None, row['Router'], row['Source']) for row in decoded['edges'] if row['Router'] != '-'),
            *([(None, *decoded['application_binding'])] if decoded['application_binding'] is not None else [])]


def _bind(typed, decoded, bindings):
    """Import plan-bound modules normally and resolve declared callables."""
    by_source = {row['source']: row for row in bindings}
    modules = {}
    for row in bindings:
        try:
            modules[row['module']] = importlib.import_module(row['module'])
        except (ImportError, ValueError) as error:
            raise ArtifactRejected(f"cannot import bound module {row['module']}: {error}") from error
    callables, registry = {}, {}
    for node, name, source in _demanded(typed, decoded):
        try:
            module = modules[by_source[_logical(source)]['module']]
        except KeyError as error:
            raise ArtifactRejected('plan does not name a binding for a declared callable') from error
        function = getattr(module, name, None)
        if (not callable(function) or getattr(function, '__module__', None) != module.__name__
                or getattr(function, '__qualname__', None) != name):
            raise ArtifactRejected(f'{module.__name__} does not define declared binding {name}')
        if node is not None:
            if registry.setdefault(name, function) is not function:
                raise ArtifactRejected(f'registry id {name} is bound to different callables')
            callables[node] = function
    return MappingProxyType(modules), MappingProxyType(callables)


class _Compiled:
    origin: ClassVar[str] = 'compiled'

    def verify_sources(self):
        """Compiled plans have no runtime source verification."""

    def resource(self, logical_path):
        for item in self.resources:
            if item.logical_path == logical_path and item.content_bytes is not None:
                return item.content_bytes
        raise KeyError(logical_path)


@dataclass(frozen=True)
class CompiledDefinition(_Compiled):
    version: ClassVar[int] = 1
    graph_id: str
    entry: PurePosixPath
    entry_root: str
    roots: object
    replacements: tuple
    resources: tuple
    definition_id: str
    compiled_plan_identity: str
    plan_digest: str
    layout: Layout
    contents: object
    selected_declarations: object
    declaration_fragments: object
    tool_usages: tuple
    bindings: tuple
    modules: object = repr(False)
    callables: object = repr(False)
    imports: tuple = ()
    declaration_entry: PurePosixPath | None = None

    def native_rejection(self, function, step):
        return None

    def registry(self):
        from .execution import NodeFunction
        return {row['Registry']: NodeFunction(self.callables[row['ID']], row['Kind'])
                for row in self.layout.tables['Functions']}

    def workflow(self, *, conditions=None, config=None, services=None):
        from .execution import Workflow
        return Workflow(self, self.registry(), conditions, config=config, services=services)


@dataclass(frozen=True)
class CompiledStateDefinition(_Compiled):
    version: ClassVar[int] = 1
    graph_id: str
    entry: PurePosixPath
    entry_root: str
    roots: object
    replacements: tuple
    resources: tuple
    declared_resources: object
    helper_sources: tuple
    native_dependencies: object
    timeouts: tuple
    schema_name: str
    schema_source: PurePosixPath
    nodes: tuple
    edges: tuple
    contents: object
    definition_id: str
    compiled_plan_identity: str
    plan_digest: str
    layout: Layout
    selected_declarations: object
    declaration_fragments: object
    tool_usages: tuple
    bindings: tuple
    modules: object = repr(False)
    callables: object = repr(False)
    application_binding: tuple | None = None
    imports: tuple = ()
    declaration_entry: PurePosixPath | None = None

    def load_catalog(self, *, packaged_proof=None, config=None):
        from .model import NativeCatalog
        from .configuration import resolve_state_timeouts
        values, keys = resolve_state_timeouts(self, config)
        sources = {self.schema_source, *(row['Source'] for row in self.nodes),
                   *(row['Source'] for row in self.edges if row['Source'] is not None)}
        if self.application_binding is not None:
            sources.add(self.application_binding[1])
        by_source = {row['source']: row['module'] for row in self.bindings}
        namespaces = {
            source: MappingProxyType(dict(vars(self.modules[by_source[_logical(source)]])))
            for source in sources
        }
        bindings = {
            (source, name): namespaces[source].get(name)
            for _, name, source in _demanded(True, self.__dict__)
            if callable(namespaces[source].get(name))
        }
        if any((source, name) not in bindings for _, name, source in _demanded(True, self.__dict__)):
            raise ArtifactRejected('compiled catalog has an unbound declared callable')
        return NativeCatalog(self.definition_id, MappingProxyType(namespaces), values, keys,
                               frozenset(self.modules), MappingProxyType(bindings))

    def workflow(self, *, reducers, services=None, journal=None, checkpointer=None, config=None):
        from .execution import Workflow
        return Workflow(self, catalog=self.load_catalog(config=config), reducers=reducers,
                                services=services, journal=journal, checkpointer=checkpointer, config=config)


def load_artifact(plan):
    """Decode a compatible plan and import its declared modules normally."""
    if type(plan) is not bytes or len(plan) > MAX_PLAN_BYTES:
        raise ArtifactRejected('plan must be bytes within the size limit')
    value = _parse(plan)
    typed = _check_versions(value)
    try:
        decoded, _distributions, bindings = _decode_plan(value, typed)
        modules, callables = _bind(typed, decoded, bindings)
    except ArtifactRejected:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError) as error:
        raise ArtifactRejected(f'malformed plan: {error}') from error
    decoded.update(bindings=bindings, modules=modules, callables=callables, plan_digest=plan_digest(plan))
    if not typed:
        return CompiledDefinition(**decoded)
    return CompiledStateDefinition(**decoded)


__all__ = [
    'ArtifactRejected', 'CompiledDefinition', 'CompiledStateDefinition', 'load_artifact',
    'plan_digest', 'compiled_plan_identity', 'compiled_provenance', 'canonical_json',
    'PLAN_VERSION', 'RUNTIME_ABI',
]
