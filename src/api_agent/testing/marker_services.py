"""Acquire the services a pack declares for tests selected by a marker."""
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import importlib.util
from pathlib import Path
import tempfile

from .collection import repository_label


def _load(path, symbol):
    spec = importlib.util.spec_from_file_location('api_agent_marker_services_' + sha256(str(path).encode()).hexdigest()[:16], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, symbol)


@contextmanager
def acquired_test_services(context, tests, environment, *, output=None):
    """Enter each declared marker's service once, only for markers a selected test carries.

    A provider is `function(services)` returning a context manager whose value is
    the environment variables to add to every test process. `services` holds the
    owner root, the environment tests will see, a scratch directory and an
    `output(text)` callback. Services stop in reverse order, also on failure.
    """
    selected = {}
    for layer in getattr(context, 'pack_layers', ()) or (context,):
        owner = Path(layer.owner_root).resolve()
        label = repository_label(context, owner)
        for marker, path, symbol in getattr(layer, 'test_prerequisites', ()):
            if any(test.repo == label and marker in test.tags for test in tests):
                selected[(owner, marker)] = (path, symbol)
    if not selected:
        yield {}
        return
    added = {}
    with ExitStack() as stack, tempfile.TemporaryDirectory(prefix='api-agent-services-') as directory:
        for (owner, marker), (path, symbol) in selected.items():
            services = {'owner_root': owner, 'environment': {**environment, **added}, 'directory': Path(directory),
                        'output': output or (lambda text: None)}
            values = stack.enter_context(_load(path, symbol)(services))
            if (not isinstance(values, dict) or any(
                    type(key) is not str or not key.isidentifier() or type(value) is not str
                    or any(character in value for character in '\0\r\n') for key, value in values.items())):
                raise ValueError(f'Test services for {marker} must return identifier keys with single-line string values')
            if set(values) & set(added):
                raise ValueError(f'Test services for {marker} redefine environment already provided')
            added.update(values)
        yield added
