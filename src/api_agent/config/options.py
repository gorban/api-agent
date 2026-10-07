"""Schema-driven CLI parsing, preserving positional selectors and provenance."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ParsedOptions:
    values: dict
    positionals: tuple[str, ...]
    errors: tuple[str, ...]

def parse_options(schema, argv):
    values, positionals, errors = {}, [], []
    argv = list(argv)
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == '--':
            positionals.extend(argv[index:])
            break
        if token == '-h':
            values['HELP'] = 'True'
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
        if not separator:
            if declaration.type == 'bool':
                value = 'True'
                if index < len(argv) and argv[index].lower() in ('true', 'false'):
                    value = argv[index]
                    index += 1
            elif index < len(argv) and not argv[index].startswith('--'):
                value = argv[index]
                index += 1
            else:
                errors.append(f'{key}: option requires a value')
                continue
        values[key] = value
    return ParsedOptions(values, tuple(positionals), tuple(errors))
