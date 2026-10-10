"""Schema-driven CLI parsing, preserving positional selectors and provenance."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ParsedOptions:
    values: dict
    positionals: tuple[str, ...]
    errors: tuple[str, ...]

def parse_options(schema, argv):
    from .resolve import format_enum_values
    values, positionals, errors = {}, [], []
    seen = set()
    argv = list(argv)
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == '--':
            positionals.extend(argv[index:])
            break
        if token == '-h':
            if 'HELP' in seen:
                errors.append('Duplicate option --help')
            else:
                values['HELP'] = 'True'
            seen.add('HELP')
            continue
        if not token.startswith('-'):
            positionals.append(token)
            continue
        if not token.startswith('--'):
            errors.append(f'Unknown option {token}')
            continue
        key, separator, value = token[2:].partition('=')
        key = key.replace('-', '_').upper()
        declaration = schema.fields.get(key)
        if declaration is None:
            errors.append(f'Unknown option --{key.lower().replace("_", "-")}')
            continue
        duplicate = key in seen
        if duplicate:
            errors.append(f'Duplicate option --{key.lower().replace("_", "-")}')
        seen.add(key)
        if not separator:
            if declaration.type == 'bool':
                value = 'True'
                if index < len(argv) and argv[index].lower() in ('true', 'false'):
                    value = argv[index]
                    index += 1
            elif declaration.type == 'str' and {'True', 'False'} <= set(declaration.enum):
                value = 'True'
                if index < len(argv) and argv[index] in declaration.enum:
                    value = argv[index]
                    index += 1
            elif index < len(argv) and not argv[index].startswith('--'):
                value = argv[index]
                index += 1
            else:
                choices = '; expected one of ' + format_enum_values(declaration.enum) if declaration.enum else ''
                errors.append(f'{key}: option requires a value{choices}')
                continue
        if not duplicate:
            values[key] = value
    return ParsedOptions(values, tuple(positionals), tuple(errors))
