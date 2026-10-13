"""Read literal .env assignments without expansion or executing shell code."""
from pathlib import Path
import json

def decode_value(value):
    """Decode generated escaped assignments; keep legacy literal values intact."""
    marker = ' # api-agent-escaped'
    if value.endswith(marker):
        try:
            decoded = json.loads(value[:-len(marker)])
            if not isinstance(decoded, str):
                raise ValueError()
            return decoded
        except ValueError:
            raise ValueError('invalid escaped quoted value') from None
    if value.startswith(('"', "'")):
        quote = value[0]
        escaped = False
        for index, character in enumerate(value[1:], 1):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                suffix = value[index + 1:]
                if suffix and not (suffix[0].isspace() and suffix.lstrip().startswith('#')):
                    raise ValueError('invalid quoted value suffix')
                return value[1:index]
        raise ValueError('unclosed quoted value')
    return value

def read_dotenv(path):
    path = Path(path)
    if not path.exists():
        return {}
    result = {}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, separator, value = line.partition('=')
        if not separator or not key.strip().isidentifier():
            raise ValueError(f'{path}:{line_number}: expected KEY=value')
        value = value.strip()
        try:
            result[key.strip()] = decode_value(value)
        except ValueError as error:
            raise ValueError(f'{path}:{line_number}: {error}') from None
    return result
