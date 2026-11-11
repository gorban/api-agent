"""Resolve declared workflow budgets without importing source admission."""
from types import MappingProxyType
from .model import LayoutError, MAX_COMPONENT_PASSES
import tomllib

def resolve_timeouts(layout, config, definition=None):
    """Resolve only declared scope budgets from captured configuration defaults."""
    from api_agent.config.declarations import schema_from_dict, UNSET
    import tomllib

    values, keys = {}, {}
    for row in layout.tables['Timeouts']:
        try:
            source_bytes = (definition.contents[row['Source']] if definition is not None
                            else row['Source'].read_bytes())
            schema = schema_from_dict(tomllib.loads(source_bytes.decode('utf-8')))
        except (OSError, UnicodeError, ValueError) as error:
            layout.error(str(error), row['line'])
        field = schema.fields.get(row['Key'])
        if field is None or field.type not in ('int', 'float'):
            layout.error('timeout must reference a numeric configuration declaration', row['line'])
        value = config.get(row['Key'], None if field.default is UNSET else field.default)
        if value is not None and (type(value) not in (int, float) or not 0 < value < float('inf')):
            layout.error('timeout must be finite positive seconds or omitted', row['line'])
        values[row['Owner']] = value
        keys[row['Owner']] = row['Key']
    return values, keys


def declared_timeouts(definition, config=None):
    """The declared Timeouts rows of any admitted definition, resolved against ``config``."""
    config = config or {}
    if getattr(definition, 'schema_source', None) is not None:
        values, _ = resolve_state_timeouts(definition, config)
    else:
        values, _ = resolve_timeouts(definition.layout, config, definition)
    return dict(values)


def resolve_component_passes(layout, config):
    """Resolve captured component limits against explicit effective configuration."""
    from .model import MAX_COMPONENT_PASSES

    passes, constants = {}, {}
    for row in layout.tables.get('Components', ()):
        key = row.get('max_passes_key')
        default = row.get('max_passes_default')
        if default is None:
            default = int(row['Max passes'])
        value = config.get(key, default) if key is not None else default
        if type(value) is not int or not 1 <= value <= MAX_COMPONENT_PASSES:
            layout.error(f'Max passes must be an integer from 1 to {MAX_COMPONENT_PASSES}', row.get('line'))
        if row['Guard'] == '-' and value != 1:
            layout.error('unguarded component requires one pass', row.get('line'))
        passes[row['ID']] = value
        if key is not None:
            constants[key] = value
    return passes, constants


def resolve_state_timeouts(definition, config=None):
    """Validate declared timeout schema and selected values before native effects."""
    from api_agent.config.declarations import UNSET, schema_from_dict
    config = config or {}
    values = {}
    keys = {}
    for row in definition.timeouts:
        try:
            declaration = schema_from_dict(tomllib.loads(definition.contents[row['Source']].decode('utf-8')))
        except (UnicodeError, ValueError) as error:
            raise LayoutError(f'{row["Source"]}:{row["line"]}: invalid timeout declaration: {error}') from error
        field = declaration.fields.get(row['Key'])
        if field is None or field.type not in ('int', 'float'):
            raise LayoutError(f'{row["Source"]}:{row["line"]}: timeout key must be numeric')
        value = config.get(row['Key'], None if field.default is UNSET else field.default)
        if value is not None and (type(value) not in (int, float) or not 0 < value < float('inf')):
            raise LayoutError(f'{row["Source"]}:{row["line"]}: timeout must be finite positive seconds')
        values[row['Owner']] = value
        keys[row['Owner']] = row['Key']
    return MappingProxyType(values), MappingProxyType(keys)


def resolve_state_limits(definition, config=None):
    """Validate declared integer limits and resolve their effective values."""
    from api_agent.config.declarations import schema_from_dict
    config = config or {}
    values = {}
    for row in getattr(definition, 'limits', ()):
        where = f'{row.get("_owner", definition.entry)}:{row["line"]}'
        try:
            declaration = schema_from_dict(tomllib.loads(definition.contents[row['Source']].decode('utf-8')))
        except (UnicodeError, ValueError) as error:
            raise LayoutError(f'{where}: invalid limit declaration: {error}') from error
        field = declaration.fields.get(row['Key'])
        if field is None or field.type != 'int':
            raise LayoutError(f'{where}: limit key must name an integer configuration field')
        if type(field.default) is not int:
            raise LayoutError(f'{where}: limit requires a declared integer default')
        value = config.get(row['Key'], field.default)
        if type(value) is not int or not 0 <= value <= MAX_COMPONENT_PASSES:
            raise LayoutError(f'{where}: limit must be an integer from 0 to {MAX_COMPONENT_PASSES}')
        values[row['Key']] = value
    return MappingProxyType(values)
