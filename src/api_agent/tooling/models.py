"""Immutable operation selections and named-tool configuration plans."""
from dataclasses import dataclass, field, replace
from pathlib import Path
from functools import cache
from types import MappingProxyType

from api_agent.config.declarations import ConfigSchema


class ToolSelectionError(ValueError):
    """Selected inert declarations cannot form one coherent tool plan."""


def freeze(value):
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class OperationSelection:
    mode: str = 'application'
    test_ids: tuple[str, ...] = ()
    variants: tuple[str, ...] = ()
    graphless: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'test_ids', tuple(self.test_ids))
        object.__setattr__(self, 'variants', tuple(self.variants))
        object.__setattr__(self, 'graphless', tuple(self.graphless))
        if self.mode not in {'application', 'unit', 'integration', 'e2e', 'tests', 'collection'}:
            raise ToolSelectionError('Unknown tool operation selection')
        if any(type(item) is not str or not item for item in (*self.test_ids, *self.variants)):
            raise ToolSelectionError('Selected cases and variants require nonempty identities')
        if len(set(self.test_ids)) != len(self.test_ids) or len(set(self.variants)) != len(self.variants):
            raise ToolSelectionError('Duplicate selected case or variant')
        if set(self.graphless) - set(self.test_ids) or len(set(self.graphless)) != len(self.graphless):
            raise ToolSelectionError('Graphless admission must name distinct selected cases')
        if self.mode == 'application' and (self.test_ids or self.variants):
            raise ToolSelectionError('Application selection cannot include test cases')


def operation_selection(value):
    if value is None:
        return OperationSelection()
    if isinstance(value, OperationSelection):
        return value
    if isinstance(value, str):
        return OperationSelection('application' if value in {'application', 'run-local', 'menu'} else value)
    from api_agent.testing.collection import TestIdentity
    items = tuple(value)
    return OperationSelection('tests', tuple(item.id if type(item) is TestIdentity else item for item in items),
        graphless=tuple(item.id for item in items if type(item) is TestIdentity))


@dataclass(frozen=True)
class ToolCatalog:
    roots: object
    definitions: object
    selected_graphs: tuple[str, ...]
    selection: OperationSelection
    diagnostics: object = field(default_factory=dict)
    assets: object = field(default_factory=dict, repr=False)
    profiles: object = field(default_factory=dict)
    selected_profiles: tuple[str, ...] = ()
    case_graphs: object = field(default_factory=dict)
    case_profiles: object = field(default_factory=dict)

    def __post_init__(self):
        for name in ('roots', 'definitions', 'diagnostics', 'assets', 'profiles', 'case_graphs', 'case_profiles'):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, 'selected_graphs', tuple(self.selected_graphs))
        object.__setattr__(self, 'selected_profiles', tuple(self.selected_profiles))


@dataclass(frozen=True)
class SelectedTool:
    id: str
    interface: str
    factory: Path
    factory_name: str
    factory_bytes: bytes = field(repr=False)
    bindings: object
    local_values: object = field(default_factory=dict, repr=False)
    pending: object = field(default_factory=dict)
    service_bindings: object = field(default_factory=dict)
    service_capabilities: object = field(default_factory=dict)
    prerequisite_roots: tuple[str, ...] = ()
    infrastructure: object = field(default_factory=dict)
    descriptor_path: Path | None = None
    implementation_path: Path | None = None
    admission: object = field(default=None, repr=False)
    configured_values: object = field(default_factory=dict, repr=False)
    value_sources: object = field(default_factory=dict, repr=False)
    derived_from: object = field(default_factory=dict)
    nonempty_inputs: tuple[str, ...] = ()
    executables: object = field(default_factory=dict)
    executable_assets: object = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for name in ('bindings', 'local_values', 'pending', 'service_bindings',
                     'service_capabilities', 'infrastructure', 'configured_values',
                     'value_sources', 'derived_from', 'executables', 'executable_assets'):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, 'prerequisite_roots', tuple(self.prerequisite_roots))
        object.__setattr__(self, 'nonempty_inputs', tuple(self.nonempty_inputs))


@dataclass(frozen=True)
class ToolPlan:
    tools: tuple[SelectedTool, ...]
    schema: ConfigSchema
    bindings: object
    definitions: tuple
    descriptor_id: str
    operation_id: str
    captured_assets: object = field(default_factory=dict, repr=False)
    infrastructure_contributions: tuple = ()
    active_fields: tuple[str, ...] = ()
    tool_fields: tuple[str, ...] = ()
    known_fields: tuple[str, ...] = ()
    inactive_fields: tuple[str, ...] = ()
    shared_primary: object = field(default_factory=dict)
    catalog: ToolCatalog | None = None
    config: object = field(default=None, repr=False)
    selection: OperationSelection = field(default_factory=OperationSelection)
    option_schema: ConfigSchema | None = None
    case_definitions: object = field(default_factory=dict)
    case_shared_primary: object = field(default_factory=dict)

    def __post_init__(self):
        for name in ('bindings', 'captured_assets', 'shared_primary', 'case_definitions', 'case_shared_primary'):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        for name in ('tools', 'definitions', 'infrastructure_contributions', 'active_fields', 'tool_fields', 'known_fields', 'inactive_fields'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        def snapshot_fields(declarations):
            return {name: replace(declaration, default=freeze(declaration.default),
                applies_when=freeze(declaration.applies_when), required_when=freeze(declaration.required_when),
                conditional_defaults=freeze(declaration.conditional_defaults), enum=freeze(declaration.enum))
                for name, declaration in declarations.items()}
        def snapshot(schema):
            return ConfigSchema(freeze(snapshot_fields(schema.fields)), schema.version,
                                freeze(snapshot_fields(schema.deferred_requirements)))
        object.__setattr__(self, 'schema', snapshot(self.schema))
        if self.option_schema is not None:
            object.__setattr__(self, 'option_schema', snapshot(self.option_schema))

    @property
    def review_fields(self):
        return tuple(name for name, declaration in self.schema.fields.items()
                     if declaration.acknowledge_default and name in self.active_fields)

    def for_case(self, case_id):
        """Narrow trusted parent admission without discovering another resource."""
        if case_id not in self.case_definitions:
            raise ToolSelectionError('Case is outside the admitted test selection')
        identities = set(self.case_definitions[case_id])
        definitions = tuple(item for item in self.definitions if item.definition_id in identities)
        bindings = {key: item for key, item in self.bindings.items() if key[0] in identities}
        tool_ids = set(bindings.values())
        shared = self.case_shared_primary.get(case_id, {})
        if any(secondary not in tool_ids or primary not in tool_ids for secondary, primary in shared.items()):
            raise ToolSelectionError('Case shared-primary profile crosses its admitted graph closure')
        tools = tuple(item for item in self.tools if item.id in tool_ids)
        paths = {path for definition in definitions for path in definition.contents if isinstance(path, Path)}
        for tool in tools:
            paths.update(path for path in (tool.descriptor_path, tool.implementation_path, tool.factory) if path is not None)
            paths.update(tool.factory.parent / name for name in tool.admission.sources)
            paths.update(tool.admission.assets)
            paths.update(tool.executable_assets)
            paths.update(binding['declaration'] for binding in tool.pending.values())
        scoped = replace(self, tools=tools, definitions=definitions, bindings=bindings,
            shared_primary=shared, case_shared_primary={case_id: shared},
            case_definitions={case_id: tuple(self.case_definitions[case_id])},
            selection=OperationSelection('tests', (case_id,)), catalog=None)
        from .services import selected_contributions
        contributions = selected_contributions(replace(scoped, config=None))
        owners = {tool.infrastructure['owner']: tool.infrastructure['owner_root']
                  for tool in self.tools if tool.infrastructure}
        for service in contributions:
            paths.update(owners[service.owner] / asset.relative_path for asset in
                (service.declaration_source, service.compose_source, *service.assets))
        return replace(scoped, infrastructure_contributions=contributions,
            captured_assets={path: body for path, body in self.captured_assets.items() if path in paths})

    def definition_for_case(self, case_id):
        definitions = self.for_case(case_id).definitions
        if len(definitions) != 1:
            raise ToolSelectionError('Case does not have exactly one admitted workflow definition')
        return definitions[0]

    def to_wire(self):
        """Encode only the trusted selected closure for a worker packet."""
        return {'version': 1, 'plan': _wire_encode(replace(self, catalog=None, config=None))}

    @classmethod
    def from_wire(cls, packet):
        """Restore captured bytes without discovering files or importing factories."""
        if type(packet) is not dict or set(packet) != {'version', 'plan'} or type(packet['version']) is not int or packet['version'] != 1:
            raise ToolSelectionError('Invalid captured tool plan packet')
        plan = _wire_decode(packet['plan'], [0, 0])
        if not isinstance(plan, cls):
            raise ToolSelectionError('Captured packet does not contain a tool plan')
        return plan

    def bind_values(self, result):
        """Project final resolved values, retaining configured and derived provenance."""
        tools = {}
        for tool in self.tools:
            values, sources = {}, {}
            for slot, alias in tool.bindings.items():
                if alias in result.fields:
                    resolved = result.fields[alias]
                    values[slot] = resolved.value
                    sources[slot] = {'source': resolved.source, 'explicit': resolved.explicit,
                                     'field': alias}
            pending = {slot: binding for slot, binding in tool.pending.items()
                       if slot in values and values[slot] == ''}
            for binding in pending.values():
                for name, expected in binding.get('activation', {}).items():
                    actual = result.fields.get(name)
                    if actual is not None and actual.explicit and actual.value != expected:
                        raise ToolSelectionError('Explicit controller disables a required local tool output')
            tools[tool.id] = replace(tool, local_values=values, configured_values=values,
                                     value_sources=sources, pending=pending)
        for secondary, primary in self.shared_primary.items():
            target, source = tools[secondary], tools[primary]
            values, provenance, derived = dict(target.local_values), dict(target.value_sources), {}
            pending = dict(target.pending)
            for slot in ('ENDPOINT', 'HEADERS'):
                if slot not in target.bindings or slot not in source.bindings:
                    continue
                alias = target.bindings[slot]
                explicit = result.fields.get(alias)
                if explicit is not None and explicit.explicit and explicit.value != source.local_values.get(slot):
                    raise ToolSelectionError('Explicit secondary configuration conflicts with shared primary')
                if slot in source.local_values:
                    values[slot] = source.local_values[slot]
                    provenance[slot] = {'source': 'derived', 'instance': primary,
                                         'field': source.bindings[slot],
                                         'configured_source': explicit.source if explicit else None,
                                         'configured_explicit': bool(explicit and explicit.explicit)}
                    derived[slot] = primary
                    if slot in source.pending:
                        pending[slot] = source.pending[slot]
                    else:
                        pending.pop(slot, None)
            tools[secondary] = replace(target, local_values=values, pending=pending,
                                        value_sources=provenance, derived_from=derived)
        for tool in tools.values():
            for slot, alias in tool.bindings.items():
                if alias not in result.deferred_required_fields:
                    continue
                declaration = self.schema.deferred_requirements[alias]
                if slot not in tool.local_values or declaration.type == 'path' and tool.local_values[slot] == '':
                    raise ToolSelectionError(f'{alias}: required effective tool input')
        # Implementations own this policy. Check after shared-primary derivation;
        # an admitted exact blank local output is provisionally symbolic.
        for tool in tools.values():
            for slot in tool.nonempty_inputs:
                if tool.bindings[slot] not in result.active_fields:
                    continue
                value = tool.local_values.get(slot)
                if isinstance(value, str) and not value.strip():
                    if value == '' and slot in tool.pending:
                        continue
                    raise ToolSelectionError(f'{tool.bindings[slot]}: selected tool input requires a nonempty string')
        return replace(self, tools=tuple(tools[tool.id] for tool in self.tools), config=result)


@cache
def _wire_types():
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.workflows.definition import (WorkflowDefinition, Replacement, Resource,
                                                InlinePromptRef, ToolUsage)
    from api_agent.workflows.stateful import StatefulDefinition
    from api_agent.workflows.layout import Layout, Step
    from api_agent.runtime.infrastructure_contract import (CapturedAsset, Activation, Readiness,
        EndpointOutput, Seed, InfrastructureService)
    from .admission import FactoryAdmission
    classes = (ToolPlan, SelectedTool, OperationSelection, ConfigSchema, FieldDeclaration, ConfigResult, ResolvedValue,
               WorkflowDefinition, StatefulDefinition, Replacement, Resource, InlinePromptRef,
               ToolUsage, Layout, Step, CapturedAsset, Activation, Readiness, EndpointOutput,
               Seed, InfrastructureService, FactoryAdmission)
    return {item.__name__: item for item in classes}


def _wire_encode(value):
    import base64
    from dataclasses import fields, is_dataclass
    from collections.abc import Mapping
    from api_agent.config.declarations import UNSET
    if value is UNSET:
        return {'unset': True}
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Path):
        return {'path': str(value)}
    if isinstance(value, bytes):
        return {'bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, Mapping):
        return {'map': [[_wire_encode(key), _wire_encode(item)] for key, item in value.items()]}
    if isinstance(value, (tuple, list)):
        return {'tuple': [_wire_encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {'set': [_wire_encode(item) for item in sorted(value)]}
    if is_dataclass(value) and type(value).__name__ in _wire_types():
        return {'class': type(value).__name__, 'fields': {item.name: _wire_encode(getattr(value, item.name))
                for item in fields(value)}}
    raise ToolSelectionError('Unsupported captured tool plan value')


def _wire_decode(value, budget, depth=0):
    import base64
    from dataclasses import fields
    from api_agent.config.declarations import UNSET
    budget[0] += 1
    if budget[0] > 300000 or depth > 128:
        raise ToolSelectionError('Captured tool packet exceeds structural budget')
    if value is None or type(value) in (bool, int, float, str):
        if isinstance(value, str):
            budget[1] += len(value.encode('utf-8'))
            if budget[1] > 256 * 1024 * 1024:
                raise ToolSelectionError('Captured tool packet exceeds byte budget')
        return value
    if type(value) is not dict:
        raise ToolSelectionError('Invalid captured tool packet value')
    decode = lambda item: _wire_decode(item, budget, depth + 1)
    if set(value) == {'unset'} and value['unset'] is True:
        return UNSET
    if set(value) == {'path'} and type(value['path']) is str:
        text = value['path']
        if (not text or len(text) > 4096 or any(ord(char) < 32 for char in text)
                or Path(text).as_posix() != text):
            raise ToolSelectionError('Invalid captured tool path')
        budget[1] += len(text.encode('utf-8'))
        if budget[1] > 256 * 1024 * 1024:
            raise ToolSelectionError('Captured tool packet exceeds byte budget')
        return Path(text)
    if set(value) == {'bytes'} and type(value['bytes']) is str and len(value['bytes']) <= 96 * 1024 * 1024:
        try:
            body = base64.b64decode(value['bytes'], validate=True)
        except ValueError:
            raise ToolSelectionError('Invalid captured tool bytes') from None
        budget[1] += len(body)
        if budget[1] > 256 * 1024 * 1024:
            raise ToolSelectionError('Captured tool packet exceeds byte budget')
        return body
    if set(value) == {'map'} and type(value['map']) is list:
        pairs = value['map']
        if any(type(pair) is not list or len(pair) != 2 for pair in pairs):
            raise ToolSelectionError('Invalid captured tool mapping')
        try:
            result = {decode(pair[0]): decode(pair[1]) for pair in pairs}
        except TypeError:
            raise ToolSelectionError('Invalid captured tool mapping key') from None
        if len(result) != len(pairs):
            raise ToolSelectionError('Duplicate captured tool mapping key')
        return freeze(result)
    for tag in ('tuple', 'set'):
        if set(value) == {tag} and type(value[tag]) is list:
            items = tuple(decode(item) for item in value[tag])
            try:
                return frozenset(items) if tag == 'set' else items
            except TypeError:
                raise ToolSelectionError('Invalid captured tool set member') from None
    if set(value) == {'class', 'fields'} and type(value['class']) is str and type(value['fields']) is dict:
        cls = _wire_types().get(value['class'])
        if cls is None or set(value['fields']) != {item.name for item in fields(cls)}:
            raise ToolSelectionError('Invalid captured tool model')
        try:
            return cls(**{name: decode(item) for name, item in value['fields'].items()})
        except (TypeError, ValueError):
            raise ToolSelectionError('Invalid captured tool model fields') from None
    raise ToolSelectionError('Invalid captured tool packet tag')
