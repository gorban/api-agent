"""Atomic literal dotenv edits, without evaluating shell syntax."""
import json
import os
from pathlib import Path
import re
import tempfile

from .dotenv import read_dotenv

_ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=')


def serialize_value(value):
    """Return external typed text (JSON objects remain JSON, paths remain text)."""
    if type(value) is bool:
        return 'True' if value else 'False'
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return str(value)


def _quoted(text):
    if not isinstance(text, str) or '\n' in text or '\r' in text or '\x00' in text:
        raise ValueError('dotenv values must be single-line text')
    # The literal reader strips only the outer quotes, with no escape decoding.
    return '"' + text + '"'


class DotenvEditor:
    def __init__(self, path):
        self.path = Path(path)

    def keys(self):
        return set(read_dotenv(self.path))

    def update(self, values):
        self._edit(values, ())

    def delete(self, names):
        self._edit({}, names)

    def _edit(self, values, names):
        values = dict(values)
        removed = set(names) | set(values)
        for key in removed:
            if not isinstance(key, str) or not key.isidentifier() or not key.isascii():
                raise ValueError('invalid dotenv key')
        assignments = {key: f'{key}={_quoted(value)}\n' for key, value in values.items()}
        if self.path.is_symlink():
            raise ValueError(f'Refusing to edit symlink: {self.path}')
        lines = self.path.read_text().splitlines(keepends=True) if self.path.exists() else []
        output, written = [], set()
        for line in lines:
            match = _ASSIGNMENT.match(line)
            key = match.group(1) if match else None
            if key in removed:
                if key in assignments and key not in written:
                    output.append(assignments[key])
                    written.add(key)
            else:
                output.append(line)
        for key, assignment in assignments.items():
            if key not in written:
                if output and not output[-1].endswith(('\n', '\r')):
                    output[-1] += '\n'
                output.append(assignment)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f'.{self.path.name}.', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(''.join(output))
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.is_symlink():
                raise ValueError(f'Refusing to edit symlink: {self.path}')
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
