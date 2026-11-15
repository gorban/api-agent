"""Fixed optional-tracing notices, independent of diagnostic content permission."""
import json
import os

_MESSAGES = {
    'setup': 'WARNING: Tracing setup failed; continuing without tracing.',
    'delivery': 'WARNING: Trace delivery failed; some traces may be missing.',
    'cleanup': 'WARNING: Tracing cleanup is pending; resources remain owned for retry.',
    'busy': 'WARNING: Tracing is unavailable in this worker; continuing without tracing.',
}


def message(code):
    return _MESSAGES.get(code) if type(code) is str else None


def emit_worker(code):
    """Send only an admitted code to the owning integration runner, if present."""
    if message(code) is None:
        return
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    if not path:
        return
    node = os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0]
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': 'trace_warning', 'node': node, 'code': code}) + '\n')
    except OSError:
        # Optional reporting cannot change the functional result.
        pass
