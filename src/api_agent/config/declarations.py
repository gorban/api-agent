"""Versioned, strict configuration declarations (no application dependencies)."""
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
import tomllib

UNSET = object()
TYPES = {'bool', 'int', 'str', 'path', 'jsonobject'}

@dataclass(frozen=True)
class FieldDeclaration:
    name: str
    type: str
    override: bool = False
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
    pack_only: bool = False
    validation: str | None = None
    default_source: str = 'shared_default'

@dataclass(frozen=True)
class ConfigSchema:
    fields: Mapping[str, FieldDeclaration]
    version: int = 1

def schema_from_dict(data: Mapping[str, Any], *, source: str = 'shared_default',
                     base_schema: ConfigSchema | None = None) -> ConfigSchema:
    if set(data) - {'version', 'fields'} or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('Configuration declaration requires version = 1 and fields only')
    if not isinstance(data.get('fields', {}), dict):
        raise ValueError('Configuration declaration requires a fields table')
    declarations = {}
    allowed = set(FieldDeclaration.__dataclass_fields__) - {'name', 'default_source'}
    for name, spec in data.get('fields', {}).items():
        if not name.isidentifier() or name != name.upper() or not isinstance(spec, dict):
            raise ValueError(f'Invalid configuration field {name!r}')
        if set(spec) - allowed or spec.get('type') not in TYPES:
            raise ValueError(f'{name}: unknown metadata or missing/invalid type')
        spec = dict(spec)
        for flag in ('override', 'required', 'review_optional', 'allow_empty', 'acknowledge_default', 'pack_only'):
            if flag in spec and type(spec[flag]) is not bool:
                raise ValueError(f'{name}: {flag} must be boolean')
        if spec.get('anchor') not in (None, 'pack_root', 'owner_root', 'base_root'):
            raise ValueError(f'{name}: invalid path anchor')
        if spec['type'] == 'path' and not spec.get('anchor'):
            raise ValueError(f'{name}: path requires an explicit anchor')
        if spec.get('allow_empty') and spec['type'] != 'path':
            raise ValueError(f'{name}: allow_empty requires path type')
        if spec.get('validation') not in (None, 'nonnegative', 'positive', 'resolution'):
            raise ValueError(f'{name}: unknown validation rule')
        if spec.get('validation') == 'nonnegative' and spec['type'] != 'int':
            raise ValueError(f'{name}: nonnegative validation requires int type')
        if spec.get('validation') == 'positive' and spec['type'] != 'int':
            raise ValueError(f'{name}: positive validation requires int type')
        if spec.get('validation') == 'resolution' and spec['type'] != 'str':
            raise ValueError(f'{name}: resolution validation requires str type')
        for metadata in ('enum', 'conditional_defaults'):
            if metadata in spec and not isinstance(spec[metadata], list):
                raise ValueError(f'{name}: {metadata} must be an array')
        if 'help' in spec and not isinstance(spec['help'], str):
            raise ValueError(f'{name}: help must be text')
        spec['enum'] = tuple(spec.get('enum', ()))
        spec['conditional_defaults'] = tuple(spec.get('conditional_defaults', ()))
        declarations[name] = FieldDeclaration(name=name, default_source=source, **spec)
    schema = ConfigSchema(declarations)
    controlling_fields = dict(base_schema.fields) if base_schema else {}
    controlling_fields.update(declarations)
    from .resolve import convert_value
    for declaration in declarations.values():
        for enum_value in declaration.enum:
            convert_value(replace(declaration, enum=()), enum_value, external=False, anchors=None)
        for predicate in (declaration.applies_when, declaration.required_when):
            _validate_predicate(predicate, controlling_fields)
        defaults = [] if declaration.default is UNSET else [declaration.default]
        for conditional in declaration.conditional_defaults:
            if not isinstance(conditional, dict) or set(conditional) != {'when', 'value'}:
                raise ValueError(f'{declaration.name}: conditional default needs when and value')
            _validate_predicate(conditional['when'], controlling_fields)
            defaults.append(conditional['value'])
        for value in defaults:
            convert_value(declaration, value, external=False, anchors=None)
    return schema

def _validate_predicate(predicate, fields):
    if not isinstance(predicate, dict):
        raise ValueError('Predicate must be a controlling-field equals table')
    for name, expected in predicate.items():
        if name == 'any':
            if not isinstance(expected, list) or not expected:
                raise ValueError('Predicate any requires a nonempty array of tables')
            for branch in expected:
                _validate_predicate(branch, fields)
            continue
        if name not in fields:
            raise ValueError(f'Unknown controlling field {name}')
        from .resolve import convert_value
        for value in expected if isinstance(expected, list) else [expected]:
            convert_value(fields[name], value, external=False, anchors=None)

def load_declarations(path: str | Path, *, base_schema: ConfigSchema | None = None,
                      source: str = 'shared_default') -> ConfigSchema:
    with Path(path).open('rb') as handle:
        return schema_from_dict(tomllib.load(handle), base_schema=base_schema, source=source)

def shared_declaration_path(base_root: Path | None = None) -> Path:
    """Locate human-edited source declarations or their bundled wheel copy."""
    if base_root is not None:
        return Path(base_root).resolve() / 'cfg/config.toml'
    source = Path(__file__).resolve().parents[3] / 'cfg/config.toml'
    if source.is_file():
        return source
    return Path(__file__).resolve().parents[1] / 'cfg/config.toml'


def load_shared_schema(base_root: Path | None = None) -> ConfigSchema:
    return load_declarations(shared_declaration_path(base_root))

def compose_schema(shared: ConfigSchema, extension: ConfigSchema | None = None,
                   overrides: Mapping[str, Any] | None = None) -> ConfigSchema:
    fields = dict(shared.fields)
    if extension:
        for name, declaration in extension.fields.items():
            if name in fields:
                if not declaration.override:
                    raise ValueError(f'{name}: shared-field replacement requires override = true')
                if declaration.type != fields[name].type:
                    raise ValueError(f'{name}: override must preserve the shared type')
            elif declaration.override:
                raise ValueError(f'{name}: override has no shared field')
            fields[name] = declaration
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
