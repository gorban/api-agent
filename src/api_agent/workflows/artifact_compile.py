"""Build-time compiler: admitted declarations plus source wheels -> canonical JSON plan.

This is the only module that touches the Markdown parser for artifacts. A
production process imports ``artifact`` alone. The plan carries data resources
(prompts, schemas, configuration, tool instances) and hashes of declarations
and native sources; native code is referenced as a binding into a source wheel
whose file is byte-identical to the admitted native source.
"""
import base64
from dataclasses import dataclass
import math
from pathlib import Path, PurePath
import zipfile

from .artifact import (DATA_KINDS, PLAN_FORMAT, PLAN_VERSION, RUNTIME_ABI, SOURCE_KINDS, ArtifactRejected,
                       canonical_json, compiled_plan_identity, distribution_identity, module_for_file, parse_record,
                       verify_record)
from .identity import logical_resolver
from .model import InlinePromptRef, LayoutError

MAX_WHEEL_BYTES = 256 * 1024 * 1024


class ArtifactCompileError(LayoutError):
    """A definition or wheel cannot be compiled into a verifiable plan."""


@dataclass(frozen=True)
class _Wheel:
    name: str
    version: str
    dist_info: str
    files: dict
    top_levels: tuple[str, ...]


def read_wheel(path):
    """Read a source wheel and verify its members against its own RECORD."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            if len({item.filename for item in infos}) != len(infos):
                raise ArtifactCompileError(f'{path}: duplicate wheel member')
            if sum(item.file_size for item in infos) > MAX_WHEEL_BYTES:
                raise ArtifactCompileError(f'{path}: wheel exceeds {MAX_WHEEL_BYTES} bytes')
            files = {item.filename: archive.read(item) for item in infos}
    except (OSError, zipfile.BadZipFile) as error:
        raise ArtifactCompileError(f'{path}: not a readable wheel') from error
    dist_infos = {name.split('/', 1)[0] for name in files if name.split('/', 1)[0].endswith('.dist-info')}
    if len(dist_infos) != 1:
        raise ArtifactCompileError(f'{path}: a wheel requires exactly one dist-info directory')
    dist_info = next(iter(dist_infos))
    try:
        entries = parse_record(files[f'{dist_info}/RECORD'], dist_info)
        hashes = verify_record(entries, files)
        name, version = distribution_identity(files, dist_info)
    except (KeyError, ArtifactRejected) as error:
        raise ArtifactCompileError(f'{path}: wheel is not a verifiable source wheel: {error}') from error
    top_levels = sorted({module.split('.', 1)[0] for path in hashes
                         for module in (module_for_file(path),) if module is not None})
    return _Wheel(name, version, dist_info, hashes, tuple(top_levels))


def _encoder(logical):
    def encode(value):
        kind = type(value)
        if value is None or kind in (bool, int, str):
            return value
        if kind is float and math.isfinite(value):
            return value
        if isinstance(value, PurePath):
            return {'$p': logical(value)}
        if isinstance(value, InlinePromptRef):
            return {'$i': [logical(value.owner), value.id]}
        if hasattr(value, 'items'):
            if not all(type(key) is str for key in value):
                raise ArtifactCompileError('plan mappings require string keys')
            return {'$m': [[key, encode(item)] for key, item in sorted(value.items())]}
        if isinstance(value, (list, tuple)):
            return [encode(item) for item in value]
        raise ArtifactCompileError(f'value cannot be represented in a plan: {kind.__name__}')
    return encode


def _select(resource, wheels, expected_module):
    """The one wheel file whose bytes are exactly the admitted native source."""
    candidates = [(wheel, path) for wheel in wheels for path, digest in wheel.files.items()
                  if digest == resource.sha256 and module_for_file(path) is not None]
    if len(candidates) > 1:
        candidates = [item for item in candidates if resource.logical_path.endswith('/' + item[1])]
    if len(candidates) != 1:
        raise ArtifactCompileError(f'{resource.logical_path}: expected exactly one wheel file with '
                                   f'the admitted bytes, found {len(candidates)}')
    wheel, path = candidates[0]
    if expected_module is not None and module_for_file(path) != expected_module:
        raise ArtifactCompileError(f'{resource.logical_path}: wheel module {module_for_file(path)} '
                                   f'is not the declared dependency {expected_module}')
    return wheel, path


def compile_definition(definition, wheels):
    """Compile one admitted definition; ``wheels`` supply every native module."""
    logical = logical_resolver(definition)
    encode = _encoder(logical)
    if getattr(definition, 'helper_sources', ()):
        raise ArtifactCompileError('helper native sources cannot be compiled; declare dependencies')
    if getattr(definition, 'prompts', ()) or getattr(definition, 'prompt_slots', ()):
        raise ArtifactCompileError('compiled plans do not support declared typed-node prompts')
    if getattr(definition, 'functions', ()):
        raise ArtifactCompileError('compiled plans do not support declared typed-node Functions')
    if getattr(definition, 'limits', ()) or any('Expired' in row for row in getattr(definition, 'timeouts', ())):
        raise ArtifactCompileError('compiled plans do not support declared limits and timeout expiry routing')
    installed = [read_wheel(path) for path in wheels]
    declared_modules = {logical(path): module
                        for module, path in getattr(definition, 'native_dependencies', {}).items()}
    keys = {logical(key): key for key in definition.contents}
    bindings, used, resources = [], {}, []
    for item in definition.resources:
        if item.kind in SOURCE_KINDS:
            content = None
        elif item.kind in DATA_KINDS and item.logical_path in keys:
            content = {'key': encode(keys[item.logical_path]),
                       'b64': base64.b64encode(item.content_bytes).decode('ascii')}
        else:
            raise ArtifactCompileError(f'{item.logical_path}: unsupported resource kind {item.kind} '
                                       'or no admitted key')
        resources.append({'kind': item.kind, 'path': item.logical_path, 'sha256': item.sha256,
                          'size': len(item.content_bytes), 'content': content})
        if item.kind in ('native_source', 'native_dependency'):
            wheel, file = _select(item, installed, declared_modules.get(item.logical_path))
            used[wheel.name] = wheel
            bindings.append({'source': item.logical_path, 'distribution': wheel.name,
                             'module': module_for_file(file), 'file': file, 'sha256': item.sha256})
    plan = {'format': PLAN_FORMAT, 'plan_version': PLAN_VERSION, 'runtime_abi': RUNTIME_ABI,
            'workflow_version': 1, 'state': None,
            'imports': encode(definition.imports), 'declaration_entry': encode(definition.declaration_entry), 'graph_id': definition.graph_id,
            'definition_id': definition.definition_id, 'entry_root': definition.entry_root,
            'entry': logical(definition.entry),
            'replacements': [[item.target_id, item.root_id, Path(item.entry).as_posix()]
                             for item in definition.replacements],
            'resources': resources,
            'layout': {'steps': [{'id': step.id, 'kind': step.kind, 'parent': step.parent,
                                  'source': logical(step.source), 'line': step.line}
                                 for step in definition.layout.steps],
                       'tables': encode(definition.layout.tables), 'files': encode(definition.layout.files)},
            'selected_declarations': encode(definition.selected_declarations or {}),
            'declaration_fragments': encode(definition.declaration_fragments or {}),
            'tool_usages': [encode([use.scope, use.port, use.instance, use.scope_kind, use.declaration,
                                    use.line, use.operations]) for use in definition.tool_usages],
            'bindings': sorted(bindings, key=lambda row: row['source']),
            'distributions': [{'name': wheel.name, 'version': wheel.version, 'dist_info': wheel.dist_info,
                               'top_levels': list(wheel.top_levels),
                               'files': sorted([path, digest] for path, digest in wheel.files.items())}
                              for _, wheel in sorted(used.items())]}
    typed = getattr(definition, 'schema_source', None) is not None
    if not typed and getattr(definition, 'application_binding', None) is not None:
        plan['application_binding'] = encode(definition.application_binding)
    if typed:
        plan['state'] = {'application_binding': encode(definition.application_binding),
                         'helper_sources': [], 'native_dependencies': encode(definition.native_dependencies),
                         'timeouts': encode([{key: value for key, value in row.items() if key != '_owner'}
                                             for row in definition.timeouts]),
                         'schema_name': definition.schema_name,
                         'schema_source': encode(definition.schema_source), 'nodes': encode(definition.nodes),
                         'edges': encode(definition.edges),
                         'declared_resources': {key: item.logical_path
                                                for key, item in definition.declared_resources.items()}}
    plan['compiled_plan_identity'] = compiled_plan_identity(plan)
    return canonical_json(plan)


def load_source_definition(entry, *, base_root, pack_root, replacements=(), entry_root=None):
    """Parse the selected declaration using the one workflow contract."""
    from .definition import load_definition
    return load_definition(entry, base_root=base_root, pack_root=pack_root, replacements=replacements,
                           entry_root=entry_root)


def compile_plan(entry, *, base_root, pack_root, wheels, entry_root=None, replacements=()):
    """Build time: declarations (and replacements) plus source wheels -> canonical plan bytes."""
    definition = load_source_definition(entry, base_root=base_root, pack_root=pack_root,
                                        replacements=replacements, entry_root=entry_root)
    return compile_definition(definition, wheels)


__all__ = ['ArtifactCompileError', 'compile_definition', 'compile_plan', 'load_source_definition', 'read_wheel']
