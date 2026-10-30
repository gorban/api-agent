"""Standalone bounded schema-validation subprocess protocol."""
import json
import sys
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


def main():
    sys.stdout.buffer.write(b'R')
    sys.stdout.buffer.flush()
    raw = sys.stdin.buffer.read(2097153)
    answer = b'?'
    if len(raw) <= 2097152:
        try:
            definition, value = json.loads(raw)
            valid = Draft202012Validator(definition, registry=Registry(retrieve=_no_remote)).is_valid(value)
            answer = b'1' if valid else b'0'
        except Exception:
            pass
    sys.stdout.buffer.write(answer)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
