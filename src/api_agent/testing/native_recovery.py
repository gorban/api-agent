"""Replay only native acquisition intents recorded inside parent-owned suite storage.

This local control data is never exported as test evidence. No executable hook,
ambient engine selection or filesystem-wide discovery is accepted.
"""
import json
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager
from hashlib import sha256
import sys
from uuid import uuid4
from threading import RLock

from api_agent.runtime.engine_targets import EngineTargetRegistry, RecoveredContainerRuntime
from api_agent.runtime.inventory import InventoryCatalog
from api_agent.runtime.ownership import StackScope
from .suite_lock import CleanupLeaseOwner


LABEL = 'api-agent.acquisition'
_BOUND_DIRECTORY = ContextVar('suite_native_recovery_directory', default=None)


def _private(path, *, directory=False):
    metadata = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
            or (not directory and metadata.st_size > 131072)):
        raise ValueError('Native recovery requires private local state')


def _write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 131072):
            raise ValueError('Native recovery requires private local state')
        body = stream.read(131073)
        after = os.fstat(stream.fileno())
    if (len(body) > 131072 or (before.st_size, before.st_mtime_ns) !=
            (after.st_size, after.st_mtime_ns)):
        raise ValueError('Native recovery state changed while reading')
    return json.loads(body)


def admitted_directory():
    """Use the existing protected launch snapshot, never a new environment knob."""
    if bound := _BOUND_DIRECTORY.get():
        _private(bound, directory=True)
        return bound
    if not os.environ.get('API_AGENT_INTEGRATION_CONTEXT'):
        return None
    from .provider_setup import integration_context_directory
    root = integration_context_directory() / 'native-recovery'
    _private(root, directory=True)
    return root


def record_intent(directory, scope, catalog, fingerprints, nonce):
    root = admitted_directory()
    directory = Path(directory)
    if root is None or directory.parent != root:
        return
    _private(directory, directory=True)
    _write(directory / 'recovery.json', {'version': 1, 'scope': scope.id,
        'catalog': catalog.to_wire(), 'fingerprints': dict(fingerprints), 'nonce': nonce})


def _request_digest(request):
    return sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _completed(directory):
    request = _read(directory / 'request.json')
    completion = _read(directory / 'complete.json')
    if completion != {'version': 1, 'request_sha256': _request_digest(request), 'disposed': True}:
        raise ValueError('Native command disposal acknowledgement differs from its intent')


def run_native_command(state, runner, command, **kwargs):
    """An independent supervisor retains the actual CLI's reserved process group."""
    commands = Path(state) / 'commands'
    commands.mkdir(mode=0o700, exist_ok=True)
    _private(commands, directory=True)
    intent = commands / uuid4().hex
    intent.mkdir(mode=0o700)
    request = {'version': 1, 'command': list(command),
        'environment': dict(kwargs.get('environment', {})),
        'directory': str(Path(kwargs.get('directory', state)).absolute()),
        'deadline': kwargs.get('deadline'),
        'combine_output': kwargs.get('combine_output', False),
        'output_limit': kwargs.get('output_limit')}
    if len(json.dumps(request).encode()) > 131072:
        raise ValueError('Native command intent exceeds its private state budget')
    _write(intent / 'request.json', request)
    descriptor = os.open(Path(state) / 'activity.lock',
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _private(Path(state) / 'activity.lock')
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        # Derive the installed/source package location from this admitted module;
        # the selected engine environment need not contain Python import paths.
        bootstrap = ('import sys;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[2]))
            + ');from api_agent.testing.native_command import main;main()')
        supervisor = (sys.executable, '-c', bootstrap, str(intent), str(descriptor))
        def not_started():
            # Only the canonical pre-Popen observer can establish this. A
            # supervisor that started but died without an ack stays pending.
            _write(intent / 'complete.json', {'version': 1,
                'request_sha256': _request_digest(request), 'disposed': True})
        result = runner(supervisor, **kwargs, inherited_fds=(descriptor,), on_not_started=not_started)
        _completed(intent)
        return result
    finally:
        # Never explicitly unlock the inherited open-file description: the CLI
        # keeps the same duty until its own descriptor closes.
        os.close(descriptor)


@contextmanager
def _quiescent(state):
    path = state / 'activity.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _private(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Native command acquisition remains active; recovery pending') from None
        commands = state / 'commands'
        if commands.exists() or commands.is_symlink():
            _private(commands, directory=True)
            for intent in commands.iterdir():
                _private(intent, directory=True)
                try:
                    _completed(intent)
                except FileNotFoundError:
                    raise ValueError('Native command disposal remains unconfirmed; recovery pending') from None
        yield
    finally:
        os.close(descriptor)


class NativeRecoveryOwner(CleanupLeaseOwner):
    def __init__(self, suite_directory):
        self.directory = Path(suite_directory) / 'native-recovery'
        self.workers = []
        self._lock = RLock()
        self.closed = False

    def prepare(self):
        self.directory.mkdir(mode=0o700)

    @contextmanager
    def bind(self):
        """Lend this operation's recovery root to owned preparation contexts."""
        _private(self.directory, directory=True)
        if self.closed:
            raise ValueError('Native recovery owner is closed')
        token = _BOUND_DIRECTORY.set(self.directory)
        try:
            yield
        finally:
            _BOUND_DIRECTORY.reset(token)

    def _runtimes(self, state, record):
        scope = StackScope(record['scope'])
        catalog = InventoryCatalog.from_wire(record['catalog'], scope=scope,
            authority=record['catalog']['authority'])
        registry = state / 'registry'
        if registry.exists() or registry.is_symlink():
            _private(registry, directory=True)
        recovery = RecoveredContainerRuntime(EngineTargetRegistry(registry), scope,
            state / 'ownership', inventory_catalog=catalog)
        targets = recovery.known_targets()
        if len(targets) > 1:
            raise ValueError('Native acquisition has multiple recorded engines')
        for key, _ in targets:
            recovery.select_target(key)
            yield recovery._selected()

    def recover(self):
        _private(self.directory, directory=True)
        from api_agent.runtime.processes import disposal_complete
        states = sorted(self.directory.iterdir())
        if states and any(not disposal_complete(worker) for worker in self.workers):
            raise ValueError('Native recovery requires confirmed worker disposal')
        for state in states:
            _private(state, directory=True)
            with _quiescent(state):
                path = state / 'recovery.json'
                if not path.exists() and not path.is_symlink():
                    # Allocation may be interrupted before intent or engine access.
                    continue
                _private(path)
                record = _read(path)
                if (type(record) is not dict or set(record) != {'version', 'scope', 'catalog', 'fingerprints', 'nonce'}
                        or type(record['version']) is not int or record['version'] != 1
                        or not re.fullmatch('[0-9a-f]{16}', record['scope'])
                        or not re.fullmatch('[0-9a-f]{32}', record['nonce'])
                        or type(record['fingerprints']) is not dict
                        or not record['fingerprints']
                        or any(type(name) is not str or type(value) is not str
                            or not re.fullmatch('[0-9a-f]{64}', value)
                            for name, value in record['fingerprints'].items())):
                    raise ValueError('Invalid native recovery intent')
                if record['scope'] != StackScope.from_roots(self.directory, state).id:
                    raise ValueError('Native recovery scope differs from parent-owned state')
                for runtime in self._runtimes(state, record):
                    candidates = tuple(item.id for item in runtime.inventory() if item.running)
                    if not candidates:
                        continue
                    selected = []
                    for row in runtime._inspect(candidates):
                        labels = row['Config']['Labels']
                        service = labels.get('com.docker.compose.service')
                        if (service in record['fingerprints'] and labels.get(LABEL) == record['nonce']
                                and labels.get('api-agent.config') == record['fingerprints'][service]):
                            selected.append(row['Id'])
                    if selected:
                        # Pin IDs before stop; its canonical ownership/engine/active
                        # lease checks reinspect these same immutable objects.
                        receipt = state / 'selected-ids.json'
                        if receipt.exists():
                            _private(receipt)
                            pinned = _read(receipt)
                            if (type(pinned) is not list or not pinned or
                                    any(type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value)
                                        for value in pinned)):
                                raise ValueError('Invalid native recovery immutable identities')
                            selected = [value for value in selected if value in pinned]
                        else:
                            _write(receipt, selected)
                        if selected:
                            runtime.stop(selected)
