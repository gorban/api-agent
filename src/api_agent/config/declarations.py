"""Versioned, strict configuration declarations (no application dependencies)."""
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
import tomllib

UNSET = object()
OPERATION_INTENTS = frozenset({'ui_session'})
TYPES = {'bool', 'int', 'float', 'str', 'path', 'jsonobject'}

@dataclass(frozen=True)
class FieldDeclaration:
    name: str
    type: str
    override: bool = False
    override_if_present: bool = False
    required: bool = False
    review_optional: bool = False
    allow_empty: bool = False
    default: Any = UNSET
    enum: tuple = ()
    applies_when: Mapping[str, Any] = field(default_factory=dict)
    required_when: Mapping[str, Any] = field(default_factory=dict)
    conditional_defaults: tuple = ()
    anchor: str | None = None
    help: str = ''
    acknowledge_default: bool = True
    sensitive: bool = False
    pack_only: bool = False
    validation: str | None = None
    cost_affecting: bool = False
    default_source: str = 'shared_default'
    declared_keys: frozenset[str] = frozenset()
    contribution_origin: Path | None = field(default=None, repr=False)

@dataclass(frozen=True)
class ConfigSchema:
    fields: Mapping[str, FieldDeclaration]
    version: int = 1

def schema_from_dict(data: Mapping[str, Any], *, source: str = 'shared_default',
                     base_schema: ConfigSchema | None = None,
                     predicate_context: ConfigSchema | None = None) -> ConfigSchema:
    if set(data) - {'version', 'fields'} or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('Configuration declaration requires version = 1 and fields only')
    if not isinstance(data.get('fields', {}), dict):
        raise ValueError('Configuration declaration requires a fields table')
    declarations = {}
    allowed = set(FieldDeclaration.__dataclass_fields__) - {'name', 'default_source', 'declared_keys', 'contribution_origin'}
    for name, spec in data.get('fields', {}).items():
        if not name.isidentifier() or name != name.upper() or not isinstance(spec, dict):
            raise ValueError(f'Invalid configuration field {name!r}')
        if set(spec) - allowed or spec.get('type') not in TYPES:
            raise ValueError(f'{name}: unknown metadata or missing/invalid type')
        spec = dict(spec)
        declared_keys = frozenset(spec)
        for flag in ('override', 'override_if_present', 'required', 'review_optional', 'allow_empty', 'acknowledge_default', 'pack_only', 'cost_affecting', 'sensitive'):
            if flag in spec and type(spec[flag]) is not bool:
                raise ValueError(f'{name}: {flag} must be boolean')
        if spec.get('override_if_present') and not spec.get('override'):
            raise ValueError(f'{name}: override_if_present requires override = true')
        if spec.get('anchor') not in (None, 'pack_root', 'owner_root', 'base_root'):
            raise ValueError(f'{name}: invalid path anchor')
        if spec['type'] == 'path' and not spec.get('anchor'):
            raise ValueError(f'{name}: path requires an explicit anchor')
        if spec.get('allow_empty') and spec['type'] not in {'path', 'int'}:
            raise ValueError(f'{name}: allow_empty requires path or int type')
        if spec.get('validation') not in (None, 'nonnegative', 'positive', 'resolution', 'regex'):
            raise ValueError(f'{name}: unknown validation rule')
        if spec.get('validation') == 'nonnegative' and spec['type'] not in {'int', 'float'}:
            raise ValueError(f'{name}: nonnegative validation requires int or float type')
        if spec.get('validation') == 'positive' and spec['type'] not in {'int', 'float'}:
            raise ValueError(f'{name}: positive validation requires int or float type')
        if spec.get('validation') == 'resolution' and spec['type'] != 'str':
            raise ValueError(f'{name}: resolution validation requires str type')
        if spec.get('validation') == 'regex' and spec['type'] != 'str':
            raise ValueError(f'{name}: regex validation requires str type')
        for metadata in ('enum', 'conditional_defaults'):
            if metadata in spec and not isinstance(spec[metadata], list):
                raise ValueError(f'{name}: {metadata} must be an array')
        if 'help' in spec and not isinstance(spec['help'], str):
            raise ValueError(f'{name}: help must be text')
        spec['enum'] = tuple(spec.get('enum', ()))
        spec['conditional_defaults'] = tuple(spec.get('conditional_defaults', ()))
        declarations[name] = FieldDeclaration(name=name, default_source=source,
                                               declared_keys=declared_keys, **spec)
    schema = ConfigSchema(declarations)
    controlling_fields = {name: declaration for name, declaration in predicate_context.fields.items()
        if declaration.override_if_present} if predicate_context else {}
    actual_fields = dict(base_schema.fields) if base_schema else {}
    controlling_fields.update(actual_fields)
    # Value validation sees inherited safety metadata, while the original
    # declaration retains which keys were explicit for composition authority.
    effective = {name: _inherit_constraints(actual_fields[name], declaration)
                 if name in actual_fields and declaration.override else declaration
                 for name, declaration in declarations.items()}
    controlling_fields.update(effective)
    pending_controllers = {name for name, declaration in controlling_fields.items()
        if declaration.override_if_present and (base_schema is None or name not in base_schema.fields)}
    for declaration in effective.values():
        deferred = declaration.override_if_present and (base_schema is None or declaration.name not in base_schema.fields)
        _validate_values(declaration, controlling_fields, deferred=deferred,
                         pending_controllers=pending_controllers)
    return schema


def _inherit_constraints(previous, declaration):
    if declaration.type != previous.type:
        raise ValueError(f'{declaration.name}: override must preserve the shared type')
    inherited = ('required', 'required_when', 'allow_empty', 'validation',
                 'enum', 'pack_only', 'cost_affecting', 'sensitive')
    return replace(declaration, **{key: getattr(previous, key) for key in inherited
                                  if key not in declaration.declared_keys})

def _validate_values(declaration, fields, *, deferred=False, pending_controllers=()):
    """Check structure now; late overrides need their target's value rules."""
    from .resolve import convert_value
    if declaration.sensitive and (declaration.type != 'str' or declaration.enum):
        raise ValueError(f'{declaration.name}: sensitive requires str type without enum choices')
    if not deferred:
        for enum_value in declaration.enum:
            convert_value(replace(declaration, enum=()), enum_value, external=False, anchors=None)
    for predicate, operation_allowed in ((declaration.applies_when, True), (declaration.required_when, False)):
        _validate_predicate(predicate, fields, validate_values=not deferred,
                            pending_controllers=pending_controllers, operation_allowed=operation_allowed)
    defaults = [] if declaration.default is UNSET else [declaration.default]
    for conditional in declaration.conditional_defaults:
        if not isinstance(conditional, dict) or set(conditional) != {'when', 'value'}:
            raise ValueError(f'{declaration.name}: conditional default needs when and value')
        _validate_predicate(conditional['when'], fields, validate_values=not deferred,
                            pending_controllers=pending_controllers)
        defaults.append(conditional['value'])
    if not deferred:
        for value in defaults:
            convert_value(declaration, value, external=False, anchors=None)


def _validate_predicate(predicate, fields, *, validate_values=True, pending_controllers=(), operation_allowed=False):
    if not isinstance(predicate, dict):
        raise ValueError('Predicate must be a controlling-field equals table')
    for name, expected in predicate.items():
        if name == 'any':
            if not isinstance(expected, list) or not expected:
                raise ValueError('Predicate any requires a nonempty array of tables')
            for branch in expected:
                _validate_predicate(branch, fields, validate_values=validate_values,
                                    pending_controllers=pending_controllers, operation_allowed=operation_allowed)
            continue
        if name == 'operation':
            if not operation_allowed or type(expected) is not str or expected not in OPERATION_INTENTS:
                raise ValueError('Unsupported operation intent in applicability predicate')
            continue
        if name not in fields:
            raise ValueError(f'Unknown controlling field {name}')
        from .resolve import convert_value
        if validate_values and name not in pending_controllers:
            for value in expected if isinstance(expected, list) else [expected]:
                convert_value(fields[name], value, external=False, anchors=None)

def load_declarations(path: str | Path, *, base_schema: ConfigSchema | None = None,
                      source: str = 'shared_default', predicate_context: ConfigSchema | None = None) -> ConfigSchema:
    location = Path(path).resolve()
    with location.open('rb') as handle:
        data = tomllib.load(handle)
    package_root = Path(__file__).resolve().parents[1]
    source_root = Path(__file__).resolve().parents[3]
    source_demo = source_root / 'demo/simple/cfg/config.toml'
    installed_demo = package_root / 'demo/simple/cfg/config.toml'
    if (location == installed_demo or
            (location == source_demo and source_root / 'src/api_agent' == package_root)):
        bundled = data.get('fields', {}).get('API_PROVIDER')
        if (isinstance(bundled, dict) and
                bundled.get('default') == 'src/api_agent/api_providers/graphql_mcp/factory.py:create'):
            bundled['default'] = str(package_root / 'api_providers/graphql_mcp/factory.py') + ':create'
    return schema_from_dict(data, base_schema=base_schema, source=source,
                            predicate_context=predicate_context)

def shared_declaration_path(base_root: Path | None = None) -> Path:
    """Locate human-edited source declarations or their bundled wheel copy."""
    if base_root is not None:
        return Path(base_root).resolve() / 'cfg/config.toml'
    source = Path(__file__).resolve().parents[3] / 'cfg/config.toml'
    if source.is_file():
        return source
    return Path(__file__).resolve().parents[1] / 'cfg/config.toml'


def load_shared_schema(base_root: Path | None = None) -> ConfigSchema:
    schema = load_declarations(shared_declaration_path(base_root))
    fields = dict(schema.fields)
    factory = Path(__file__).resolve().parent.parent / 'llm_providers/gpt/factory.py'
    fields['LLM_PROVIDER'] = replace(fields['LLM_PROVIDER'], default=str(factory) + ':factory')
    ui_factory = Path(__file__).resolve().parent.parent / 'ui/provider.py'
    if 'UI_PROVIDER' in fields:
        fields['UI_PROVIDER'] = replace(fields['UI_PROVIDER'], default=str(ui_factory) + ':create_provider')
    return ConfigSchema(fields)

def _compose_fields(shared: ConfigSchema, extension: ConfigSchema | None = None) -> ConfigSchema:
    fields = dict(shared.fields)
    if extension:
        for name, declaration in extension.fields.items():
            if declaration.override_if_present and name not in fields:
                continue
            if name in fields:
                if not declaration.override:
                    raise ValueError(f'{name}: shared-field replacement requires override = true')
                previous = fields[name]
                # Omitted safety metadata retains its previous authority. Explicit
                # changes may tighten it, but cannot remove an existing constraint.
                declaration = _inherit_constraints(previous, declaration)
                if (previous.required and not declaration.required
                        or previous.required_when and declaration.required_when != previous.required_when
                        or not previous.allow_empty and declaration.allow_empty
                        or previous.validation is not None and declaration.validation != previous.validation
                           and (previous.validation, declaration.validation) != ('nonnegative', 'positive')
                        or previous.enum and (not declaration.enum or
                                              any(value not in previous.enum for value in declaration.enum))
                        or previous.pack_only and not declaration.pack_only
                        or previous.cost_affecting and not declaration.cost_affecting
                        or previous.sensitive and not declaration.sensitive):
                    raise ValueError(f'{name}: override weakens an existing field constraint')
            elif declaration.override:
                raise ValueError(f'{name}: override has no shared field')
            fields[name] = declaration
    return ConfigSchema(fields)


def _validate_schema(schema, predicate_context=None):
    # A predicate may reference a sibling declared later in the same file.
    # Pending same-file controllers are lookup context, never live fields.
    context = {name: declaration for name, declaration in predicate_context.fields.items()
               if declaration.override_if_present} if predicate_context else {}
    pending = set(context) - schema.fields.keys()
    context.update(schema.fields)
    for declaration in schema.fields.values():
        _validate_values(declaration, context, pending_controllers=pending)


def compose_schema(shared: ConfigSchema, extension: ConfigSchema | None = None,
                   overrides: Mapping[str, Any] | None = None, *,
                   predicate_context: ConfigSchema | None = None) -> ConfigSchema:
    schema = _compose_fields(shared, extension)
    _validate_schema(schema, predicate_context)
    fields = dict(schema.fields)
    from .resolve import convert_value
    for name, value in (overrides or {}).items():
        if name not in fields:
            raise ValueError(f'Unknown default override {name}')
        declaration = fields[name]
        if isinstance(value, dict) and declaration.type == 'path':
            if set(value) != {'value', 'anchor'} or value['anchor'] not in ('pack_root', 'owner_root', 'base_root'):
                raise ValueError(f'{name}: path override needs value and explicit anchor')
            declaration = replace(declaration, anchor=value['anchor'])
            value = value['value']
        convert_value(declaration, value, external=False, anchors=None)
        fields[name] = replace(declaration, default=value, conditional_defaults=(), default_source='pack_default')
    return ConfigSchema(fields)
