"""Explicit, immutable engine targets and bounded local recovery records.

The caller owns the private registry directory. Records include local transport
configuration and must never be exported as diagnostics or packaged artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
from types import MappingProxyType

from .compose import controlled_engine_environment


class EngineTargetError(RuntimeError):
    """A selected target cannot safely receive an operation."""


def _verify(environment, *, executable, cancelled):
    from .reporting_engine import verified_engine_binding
    return verified_engine_binding(environment, executable=executable, cancelled=cancelled)


@dataclass(frozen=True, repr=False)
class EngineTarget:
    """A CLI, controlled transport and corroborated daemon/socket generation."""
    label: str
    executable: str = field(repr=False)
    executable_digest: str = field(repr=False)
    identity: str
    environment: object = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.label, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.label)
                or not isinstance(self.executable, str) or not Path(self.executable).is_absolute()
                or not isinstance(self.identity, str) or not re.fullmatch('[0-9a-f]{64}', self.identity)
                or not isinstance(self.executable_digest, str)
                or not re.fullmatch('[0-9a-f]{64}', self.executable_digest)):
            raise EngineTargetError('Invalid recorded engine target.')
        try:
            environment = controlled_engine_environment(self.environment)
            transport = environment['DOCKER_HOST']
            if (not transport.startswith('unix:///')
                    or environment.get('CONTAINER_HOST', transport) != transport):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise EngineTargetError('Invalid recorded engine transport.') from error
        object.__setattr__(self, 'environment', MappingProxyType(environment))

    @classmethod
    def capture(cls, label, executable, environment, *, verifier=_verify, cancelled=lambda: False):
        """Resolve once; a context/default is replaced by verified Unix transport."""
        environment = controlled_engine_environment(environment)
        selected = shutil.which(executable, path=environment.get('PATH', os.defpath))
        if selected is None:
            raise EngineTargetError('Selected engine executable is unavailable.')
        try:
            # Preserve argv[0] for symlinked/multicall engine shims. Pin and
            # revalidate the bytes reached through this invocation path below.
            selected = str(Path(selected).absolute())
            digest = sha256(Path(selected).read_bytes()).hexdigest()
            identity, transport = verifier(environment, executable=selected, cancelled=cancelled)
        except Exception as error:
            raise EngineTargetError('Selected engine verification failed.') from error
        return cls(label, selected, digest, identity,
                   {**environment, 'DOCKER_HOST': transport, 'CONTAINER_HOST': transport})

    @property
    def key(self):
        record = self.record()
        record.pop('label')
        return sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()

    @property
    def lease_key(self):
        """Share exclusion across execution variants of one verified binding."""
        binding = (self.identity, self.environment['DOCKER_HOST'])
        return sha256(json.dumps(binding).encode()).hexdigest()

    def record(self):
        return {'label': self.label, 'executable': self.executable,
                'executable_digest': self.executable_digest, 'identity': self.identity,
                'environment': dict(self.environment)}

    def verify(self, *, verifier=_verify, cancelled=lambda: False):
        """Refuse stale generations and replaced CLIs; never adopt a new default."""
        try:
            if sha256(Path(self.executable).read_bytes()).hexdigest() != self.executable_digest:
                raise ValueError()
            identity, transport = verifier(dict(self.environment), executable=self.executable, cancelled=cancelled)
            if identity != self.identity or transport != self.environment['DOCKER_HOST']:
                raise ValueError()
        except Exception as error:
            raise EngineTargetError('Selected engine is unavailable or its identity changed.') from error


    def run(self, arguments, *, directory, deadline, cancelled=lambda: False, runner=None):
        """Run one bounded child against this target; return scrubbed stdout."""
        from .prerequisites import run_command
        self.verify(cancelled=cancelled)
        return (runner or run_command)((self.executable, *arguments), environment=self.environment,
                                      directory=directory, deadline=deadline, cancelled=cancelled)


class EngineTargetRegistry:
    """Only targets actually used by this owner; no host-context discovery."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def _open(self, name, flags):
        try:
            descriptor = os.open(self.directory / name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except FileNotFoundError:
            raise
        except OSError:
            raise EngineTargetError('Engine recovery record is unavailable.') from None
        evidence = os.fstat(descriptor)
        if (not stat.S_ISREG(evidence.st_mode) or evidence.st_uid != os.getuid()
                or evidence.st_mode & 0o077 or evidence.st_size > 131072):
            os.close(descriptor)
            raise EngineTargetError('Unsafe engine recovery record.')
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
        return descriptor

    def targets(self):
        try:
            descriptor = self._open('engine-targets.json', os.O_RDONLY)
        except FileNotFoundError:
            return ()
        try:
            with os.fdopen(descriptor) as stream:
                document = json.load(stream)
            if (set(document) != {'version', 'targets'} or type(document['version']) is not int or document['version'] != 1
                    or not isinstance(document['targets'], list) or len(document['targets']) > 16):
                raise ValueError()
            targets = tuple(EngineTarget(**record) for record in document['targets'])
            if len({target.key for target in targets}) != len(targets):
                raise ValueError()
            return targets
        except (ValueError, TypeError) as error:
            raise EngineTargetError('Invalid engine recovery record.') from error

    def remember(self, target):
        """Durably record before acquisition so abrupt process exit is recoverable."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        evidence = self.directory.lstat()
        if (not stat.S_ISDIR(evidence.st_mode) or evidence.st_uid != os.getuid()
                or evidence.st_mode & 0o077):
            raise EngineTargetError('Unsafe engine recovery directory.')
        descriptor = self._open('engine-targets.lock', os.O_CREAT | os.O_RDWR)
        with os.fdopen(descriptor, 'r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EngineTargetError("Engine recovery registry is busy; retry later.") from None
            targets = {item.key: item for item in self.targets()}
            targets[target.key] = target
            if len(targets) > 16:
                raise EngineTargetError('Engine recovery target limit reached.')
            descriptor = self._open('engine-targets.pending', os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
            with os.fdopen(descriptor, 'w') as stream:
                json.dump({'version': 1, 'targets': [item.record() for item in targets.values()]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.directory / 'engine-targets.pending', self.directory / 'engine-targets.json')


class RecoveredContainerRuntime:
    """Select one recorded engine; never fall back to a current host default."""
    def __init__(self, registry, scope, ownership_directory, *, inventory_catalog=None):
        self.registry, self.scope = registry, scope
        self.inventory_catalog = inventory_catalog
        self.ownership_directory = ownership_directory
        self.selected_runtime = None
        self.selected_key = None
        self.selected_label = None

    def known_targets(self):
        return tuple((target.key, target.label) for target in self.registry.targets())

    def select_target(self, key):
        from .containers import ContainerRuntime
        target = next((item for item in self.registry.targets() if item.key == key), None)
        if target is None:
            raise EngineTargetError('Select a known engine target.')
        self.selected_runtime = ContainerRuntime(scope=self.scope,
            ownership_directory=self.ownership_directory, engine_target=target, inventory_catalog=self.inventory_catalog)
        self.selected_key, self.selected_label = target.key, target.label

    def _selected(self):
        if self.selected_runtime is None:
            raise EngineTargetError('Select a known engine target.')
        return self.selected_runtime

    def inventory(self):
        return self._selected().inventory()

    def stop(self, identifiers):
        return self._selected().stop(identifiers)

    def logs(self, identifier, **options):
        return self._selected().logs(identifier, **options)
