"""Typed resolution with explicit provenance and conditional requirements."""
from dataclasses import dataclass
from pathlib import Path
import json
import math
import os
import re
from .declarations import UNSET
from .options import parse_options

class ConfigError(ValueError):
    pass

@dataclass(frozen=True)
class ResolvedValue:
    value: object
    source: str
    explicit: bool

@dataclass(frozen=True)
class ConfigResult:
    values: dict
    fields: dict
    positionals: tuple
    missing: tuple
    errors: tuple
    help_requested: bool = False

    def raise_for_errors(self):
        if self.errors:
            raise ConfigError('\n'.join(self.errors))
        return self

def runtime_directory_overrides(context):
    """Pin the checkout selected before imports; ordinary dotenv cannot retarget it."""
    return ({'API_AGENT_DIR': str(context.base_root)}
            if getattr(context, 'bootstrap_source', None) is not None else {})

def retain_runtime_directory(result, context):
    from dataclasses import replace
    source = getattr(context, 'bootstrap_source', None)
    if source is None or 'API_AGENT_DIR' not in result.fields:
        return result
    fields = dict(result.fields)
    fields['API_AGENT_DIR'] = replace(fields['API_AGENT_DIR'], source=source,
                                         explicit=context.bootstrap_explicit)
    return replace(result, fields=fields)

def format_enum_values(values):
    return ', '.join('<blank>' if value == '' else str(value) for value in values)

def convert_value(declaration, value, *, external, anchors):
    kind = declaration.type
    if kind == 'int' and declaration.allow_empty and value == '':
        return None
    if external:
        if not isinstance(value, str):
            raise ValueError('external values must be strings')
        if kind == 'bool':
            if value not in ('True', 'False'):
                raise ValueError('expected True or False')
            value = value == 'True'
        elif kind == 'int':
            if not re.fullmatch(r'[+-]?\d+', value):
                raise ValueError('expected integer')
            value = int(value)
        elif kind == 'float':
            try:
                value = float(value)
            except ValueError:
                raise ValueError('expected finite number') from None
        elif kind == 'jsonobject':
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError('expected JSON object') from None
    if kind == 'float' and type(value) is int:
        value = float(value)
    expected = {'bool': bool, 'int': int, 'float': float, 'str': str, 'path': str, 'jsonobject': dict}[kind]
    if type(value) is not expected:
        raise ValueError(f'expected {kind}')
    if kind == 'float' and not math.isfinite(value):
        raise ValueError('expected finite number')
    if kind == 'path' and not value.strip():
        if not declaration.allow_empty:
            raise ValueError('expected nonempty path')
        value = ''
    if declaration.enum and value not in declaration.enum:
        raise ValueError('expected one of ' + format_enum_values(declaration.enum))
    if declaration.validation == 'nonnegative' and value < 0:
        raise ValueError('expected nonnegative number' if kind == 'float' else 'expected nonnegative integer')
    if declaration.validation == 'positive' and value <= 0:
        raise ValueError('expected positive number' if kind == 'float' else 'expected positive integer')
    if declaration.validation == 'resolution' and not re.fullmatch(r'[1-9]\d*x[1-9]\d*', value):
        raise ValueError('expected positive WIDTHxHEIGHT')
    if declaration.validation == 'regex':
        from api_agent.providers.selection import compile_pattern
        compile_pattern(value)
    if kind == 'path' and value and anchors is not None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            anchor = anchors.get(declaration.anchor)
            if anchor is None or not Path(anchor).is_absolute():
                raise ValueError(f'requires absolute {declaration.anchor} anchor')
            path = Path(anchor) / path
        value = path.resolve()
    return value

def _expected_matches(actual, expected):
    # Empty optional integers resolve to None; missing controllers stay UNSET.
    return actual == expected or actual is None and expected == ''


def _matches(predicate, values, operation_intents=()):
    return all(expected in operation_intents if name == 'operation' else
               any(_matches(branch, values, operation_intents) for branch in expected) if name == 'any'
               else (any(_expected_matches(values.get(name, UNSET), value) for value in expected)
                     if isinstance(expected, list) else _expected_matches(values.get(name, UNSET), expected))
               for name, expected in predicate.items())

def _controllers(predicate):
    for name, expected in predicate.items():
        if name == 'any':
            for branch in expected:
                yield from _controllers(branch)
        elif name != 'operation':
            yield name

def resolve_config(schema, *, argv=(), environ=None, dotenv=None, anchors=None,
                   interactive=False, pack_launcher=False, overrides=None, operation_intents=()):
    from .declarations import OPERATION_INTENTS
    operation_intents = frozenset(operation_intents)
    if not operation_intents <= OPERATION_INTENTS:
        raise ConfigError('Unsupported configuration operation intent')
    options = parse_options(schema, argv)
    environ = os.environ if environ is None else environ
    sources = [('dotenv', dotenv or {}), ('environment', environ), ('cli', options.values)]
    sources.append(('interactive', overrides or {}))
    raw = {}
    for name, declaration in schema.fields.items():
        if declaration.default is not UNSET:
            raw[name] = (declaration.default, declaration.default_source, False)
    for source, mapping in sources:
        for name, value in mapping.items():
            if name in schema.fields:
                raw[name] = (value, source, True)
    fields, values, errors = {}, {}, list(options.errors)
    def resolve(name):
        if name not in raw:
            return
        value, source, explicit = raw[name]
        try:
            value = convert_value(schema.fields[name], value, external=explicit, anchors=anchors if anchors is not None else {})
        except ValueError as error:
            errors.append(f'{name}: {error}')
            return
        fields[name] = ResolvedValue(value, source, explicit)
        values[name] = value
    if 'HELP' in schema.fields:
        resolve('HELP')
    if values.get('HELP') is True:
        return ConfigResult(values, fields, options.positionals, (), tuple(errors), True)
    # Resolve all plain values first so predicate order never depends on TOML order.
    deferred_errors = {}
    completed, visiting = {'HELP'}, set()
    def resolve_defaults(name):
        if name in completed:
            return
        if name in visiting:
            raise ConfigError(f'{name}: cyclic conditional defaults')
        visiting.add(name)
        declaration = schema.fields[name]
        if name not in raw or not raw[name][2]:
            for conditional in declaration.conditional_defaults:
                for controller in _controllers(conditional['when']):
                    resolve_defaults(controller)
                if _matches(conditional['when'], values):
                    raw[name] = (conditional['value'], declaration.default_source, False)
                    break
        before = len(errors)
        resolve(name)
        if len(errors) > before:
            deferred_errors[name] = errors[before:]
            del errors[before:]
        visiting.remove(name)
        completed.add(name)
    for name in schema.fields:
        resolve_defaults(name)
    active = {name for name, declaration in schema.fields.items()
              if (not declaration.pack_only or pack_launcher) and _matches(declaration.applies_when, values, operation_intents)}
    for name in active:
        errors.extend(deferred_errors.get(name, ()))
    missing = tuple(name for name, declaration in schema.fields.items()
                    if name in active and (declaration.required or bool(declaration.required_when)
                    and _matches(declaration.required_when, values)) and name not in raw)
    if not interactive:
        errors.extend(f'{name}: required; provide {name} or --{name.lower().replace("_", "-")}' for name in missing)
    fields = {name: value for name, value in fields.items() if name in active}
    values = {name: value.value for name, value in fields.items()}
    return ConfigResult(values, fields, options.positionals, missing, tuple(errors))
