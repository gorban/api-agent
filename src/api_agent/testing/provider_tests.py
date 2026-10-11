"""Inert provider-owned test declarations and small inference selections."""
import os
from pathlib import Path
import stat
import tomllib

from api_agent.providers.contracts import SetupPrerequisiteError
from .collection import CollectionError


def load_test_declaration(root):
    root = Path(root)
    manifest = root / 'cfg/tests.toml'
    if not manifest.exists():
        return None
    if not manifest.resolve().is_relative_to(root):
        raise CollectionError(f'Provider test declaration escapes its root: {manifest}')
    try:
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('not a regular file')
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError('declaration too large')
        data = tomllib.loads(raw.decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise CollectionError(f'Cannot read provider test declaration: {manifest}') from exc
    tests = data.get('tests')
    if (set(data) != {'version', 'tests'} or type(data.get('version')) is not int
            or data['version'] != 1 or not isinstance(tests, dict)
            or set(tests) - {'integration', 'request_selections'}
            or not isinstance(tests.get('integration'), list)):
        raise CollectionError(f'Invalid provider test declaration: {manifest}')
    selections = tests.get('request_selections', [])
    if not isinstance(selections, list):
        raise CollectionError('Provider request selections must be an array')
    for entry in selections:
        if (not isinstance(entry, dict) or set(entry) != {'selected_llm', 'when'}
                or type(entry['selected_llm']) is not str or not entry['selected_llm'].strip()
                or len(entry['selected_llm']) > 256 or not entry['selected_llm'].isprintable()
                or not isinstance(entry['when'], dict)):
            raise CollectionError('Invalid provider request selection')
        for name, expected in entry['when'].items():
            choices = expected if isinstance(expected, list) else [expected]
            if (not name.isidentifier() or name != name.upper() or not choices
                    or any(type(value) not in (str, bool, int) for value in choices)):
                raise CollectionError('Invalid provider request selection condition')
    return tests


def request_selection(config, *, owner_root):
    """First matching provider test case; no runtime setting or catalog request."""
    from api_agent.providers.loading import provider_location
    selected = config.get('LLM_PROVIDER')
    if not selected:
        raise SetupPrerequisiteError('Select an LLM provider before running inference tests.')
    factory, unused = provider_location(selected, owner_root=owner_root)
    tests = load_test_declaration(factory.parent)
    for entry in (tests or {}).get('request_selections', []):
        if all(config.get(name) in (expected if isinstance(expected, list) else [expected])
               for name, expected in entry['when'].items()):
            return entry['selected_llm']
    raise SetupPrerequisiteError('The selected provider has no small inference test case for this configuration.')
