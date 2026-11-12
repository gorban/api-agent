"""Acquire the services a pack declares for tests selected by a marker."""
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import importlib.util
from pathlib import Path
import tempfile
from threading import Event, Timer
import time

from api_agent.config.process_settings import process_setting

from .collection import repository_label


def _discard(text):
    """Drop provider progress text when the caller shows no output."""


def _load(path, symbol):
    spec = importlib.util.spec_from_file_location('api_agent_marker_services_' + sha256(str(path).encode()).hexdigest()[:16], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, symbol)


@contextmanager
def acquired_test_services(context, tests, environment, *, output=None, timeout=None, cancel_event=None):
    """Enter each declared marker's service once, only for markers a selected test carries.

    A provider is `function(services)` returning a context manager whose value is
    the environment variables to add to every test process. `services` holds the
    owner root, the environment tests will see, a scratch directory, an
    `output(text)` callback, the startup `deadline` (a `time.monotonic()` value)
    and a `cancelled()` check. Starting every service, including any image build,
    shares one budget of `timeout` seconds (else `TEST_SERVICES_TIMEOUT_SECONDS`);
    a provider must stop waiting at the deadline or once `cancelled()` is true.
    A service that is not up by then, or a run cancelled through `cancel_event`
    while it starts, stops what has started and fails. Services stop in reverse
    order, also on failure.
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
    if timeout is None:
        timeout = process_setting('TEST_SERVICES_TIMEOUT_SECONDS')
    deadline = time.monotonic() + timeout
    expired = Event()
    timer = Timer(timeout, expired.set)
    timer.daemon = True
    timer.start()

    def cancelled():
        return expired.is_set() or (cancel_event is not None and cancel_event.is_set())

    def stopped(marker):
        if cancel_event is not None and cancel_event.is_set():
            return InterruptedError(f'Test services for {marker} were cancelled during startup')
        return TimeoutError(f'Test services for {marker} did not start within {timeout:g} seconds')

    added = {}
    try:
        with ExitStack() as stack, tempfile.TemporaryDirectory(prefix='api-agent-services-') as directory:
            for (owner, marker), (path, symbol) in selected.items():
                services = {'owner_root': owner, 'environment': {**environment, **added}, 'directory': Path(directory),
                            'output': output or _discard, 'deadline': deadline, 'cancelled': cancelled}
                try:
                    values = stack.enter_context(_load(path, symbol)(services))
                except Exception as error:
                    if cancelled():
                        raise stopped(marker) from error
                    raise
                if cancelled():
                    raise stopped(marker)
                if (not isinstance(values, dict) or any(
                        type(key) is not str or not key.isidentifier() or type(value) is not str
                        or any(character in value for character in '\0\r\n') for key, value in values.items())):
                    raise ValueError(f'Test services for {marker} must return identifier keys with single-line string values')
                if set(values) & set(added):
                    raise ValueError(f'Test services for {marker} redefine environment already provided')
                added.update(values)
            timer.cancel()
            yield added
    finally:
        timer.cancel()
