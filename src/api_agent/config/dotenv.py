"""Read literal .env assignments without expansion or executing shell code."""
from pathlib import Path

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
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'{path}:{line_number}: unclosed quoted value')
            value = value[1:-1]
        result[key.strip()] = value
    return result
