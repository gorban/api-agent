"""Explicit local reporting bindings, delegating leases to the original journal.

No port or label discovery grants ownership. Enrollment is an operator-selected,
read-only verification followed by an atomic local metadata registration.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from hashlib import sha256
import json
import errno
import os
from pathlib import Path
import socket
import stat
import time
from types import MappingProxyType

from api_agent.providers.progress import report_timeout_sync, independent_timeout_scope
from .compose import rebind_reporting_plan
from .ownership import StackScope
from .prerequisites import PrerequisiteError, PrerequisiteManager, _locked, _read_journal, process_identity
from .reporting_engine import engine_identity

from .reporting_metadata import _directory, _read, _write


def _assets(plan):
    return {name: sha256(value).hexdigest() for name, value in plan.assets.items()}


def _configuration(plan):
    return {key: plan.environment[key] for key in
            ('SA_LOCALSTACK_PORT', 'SA_ALLURE_S3_BUCKET', 'SA_ALLURE_S3_REGION')}


def _binding_location(record, key, registry_directory):
    """Validate the physical owner before opening its lock and again under it."""
    if (not isinstance(record, dict) or not isinstance(record.get('scope'), str)
            or len(record['scope']) != 16
            or any(char not in '0123456789abcdef' for char in record['scope'])):
        raise ValueError('Invalid scope')
    raw_directory = record['directory']
    if (not isinstance(raw_directory, str) or not Path(raw_directory).is_absolute()
            or '..' in Path(raw_directory).parts):
        raise ValueError('Invalid ownership directory')
    directory = _directory(raw_directory)
    info = directory.stat()
    if record['directory_identity'] != [info.st_dev, info.st_ino]:
        raise ValueError('Changed ownership directory')
    scope = StackScope(record['scope'])
    roots = record['roots']
    if not isinstance(roots, list) or len(roots) not in {0, 2}:
        raise ValueError('Invalid roots')
    if roots:
        original_roots = []
        for root in roots:
            path = _directory(root['path'])
            root_info = path.stat()
            if root['identity'] != [root_info.st_dev, root_info.st_ino]:
                raise ValueError('Changed root')
            original_roots.append(path)
        if StackScope.from_roots(*original_roots).id != scope.id:
            raise ValueError('Changed scope')
    elif scope.id != key or directory != registry_directory.absolute() / 'ownership':
        raise ValueError('Changed canonical owner')
    return directory, scope


def _retained_assets(manager, record, directory, deadline, cancelled, *, cleanup=False):
    """Read the registered immutable snapshot for a selected-service-equal reuse."""
    def active():
        if cancelled() or time.monotonic() >= deadline:
            raise PrerequisiteError('Retained reporting verification was cancelled or timed out.')

    active()
    names = set(manager.plan.assets)
    saved = record['assets']
    if (type(saved) is not dict or set(saved) not in (
            {'compose.json', 'empty.env', 'localstack-ready.sh'},
            {'compose.json', 'empty.env', 'localstack-ready.sh', 'jaeger-config.json'})
            or any(type(value) is not str or len(value) != 64
                   or any(char not in '0123456789abcdef' for char in value) for value in saved.values())
            or names != {'compose.json', 'empty.env', 'localstack-ready.sh', 'jaeger-config.json'}
            or any(saved[name] != sha256(manager.plan.assets[name]).hexdigest()
                   for name in ('empty.env', 'localstack-ready.sh'))):
        raise PrerequisiteError('Retained reporting assets are not eligible for verified reuse.')
    try:
        inventory = manager._inventory(deadline, cleanup=cleanup)
        active()
        if set(inventory) != set(record['ids']) or len(inventory) != 1:
            raise ValueError()
        manager._compatible(inventory, deadline, allow_stopped=True, cleanup=cleanup)
        active()
        identifier = record['ids'][0]
        inspected, = json.loads(manager._command((manager.plan.command[0], 'inspect', identifier), deadline,
                                                  cleanup=cleanup))
        active()
        if inspected['Id'] != identifier:
            raise ValueError()
        hook, = [mount for mount in inspected['Mounts']
                 if mount['Destination'] == '/etc/localstack/init/ready.d/10-api-agent-s3.sh']
        source = Path(hook['Source'])
        if not source.is_absolute() or '..' in source.parts:
            raise ValueError()
        parent = _directory(source.parent)
        if (source.name != 'localstack-ready.sh' or hook['Type'] != 'bind'
                or hook.get('RW') is not False or not parent.is_relative_to(directory)):
            raise ValueError()
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent_info = os.fstat(parent_fd)
            if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
                raise ValueError()
            discovered = set()
            with os.scandir(parent_fd) as entries:
                for count, entry in enumerate(entries, 1):
                    active()
                    if count > len(saved):
                        raise ValueError()
                    discovered.add(entry.name)
            if discovered != set(saved):
                raise ValueError()
            assets = {}
            for name in sorted(saved):
                active()
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_mode & 0o022 or info.st_size > 1024 * 1024):
                        raise ValueError()
                    with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                        body = stream.read(1024 * 1024 + 1)
                    after = os.fstat(descriptor)
                    if (len(body) > 1024 * 1024 or sha256(body).hexdigest() != saved[name]
                            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
                               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                        raise ValueError()
                    active()
                    assets[name] = body
                finally:
                    os.close(descriptor)
            current_parent = parent.stat()
            if (current_parent.st_dev, current_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino):
                raise ValueError()
        finally:
            os.close(parent_fd)
        active()
        return assets
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise PrerequisiteError('Retained reporting assets could not be verified.') from error


def _occupied(port):
    for family, host in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as client:
                client.settimeout(.5)
                if client.connect_ex((host, port)) == 0:
                    return True
        except OSError as error:
            if error.errno not in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
                raise
    return False


class SharedReportingManager:
    """Retain the underlying manager across failure and retryable release."""
    session_lifecycle = True

    def __init__(self, plan, *, engine_environment=None, cancelled=lambda: False,
                 deadline_seconds=120, registry_directory=None, identity=engine_identity,
                 manager_factory=PrerequisiteManager, occupied=_occupied):
        if plan.profiles != ('localstack',) or plan.services != ('localstack',):
            raise PrerequisiteError('Shared reporting supports only the LocalStack profile.')
        self.plan, self.environment = plan, engine_environment
        self.cancelled, self.timeout = cancelled, deadline_seconds
        if deadline_seconds <= 0:
            raise PrerequisiteError('Reporting deadline must be positive.')
        self.directory = Path(registry_directory) if registry_directory else (
            Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'api-agent/reporting')
        self.identity, self.factory, self.occupied = identity, manager_factory, occupied
        self.manager = None
        self.key = None
        self.ownership_identity = None
        self.intent = False
        self.pending_cleanup = None

    @property
    def lease(self):
        return self.manager.lease if self.manager is not None else None

    @contextmanager
    def _binding(self, *, cleanup=False):
        with independent_timeout_scope() if cleanup else nullcontext():
            with report_timeout_sync('Shared reporting ownership', self.timeout) as budget:
                engine = self.identity(self.environment, executable=self.plan.command[0],
                                       cancelled=(lambda: False) if cleanup else self.cancelled)
                endpoint = f"http://127.0.0.1:{self.plan.environment['SA_LOCALSTACK_PORT']}/"
                key = sha256(json.dumps(['localstack', engine, endpoint]).encode()).hexdigest()[:16]
                if self.key is not None and self.key != key:
                    raise PrerequisiteError('Reporting engine generation changed; retained cleanup requires verification.')
                self.key = key
                directory = _directory(self.directory, create=True)
                if directory.stat().st_mode & 0o077:
                    raise PrerequisiteError('Reporting registry directory must be private.')
                with _locked(directory, StackScope(self.key), deadline=time.monotonic() + budget.remaining,
                             cancelled=(lambda: False) if cleanup else self.cancelled, now=time.monotonic, sleep=time.sleep):
                    yield directory / (self.key + '.json'), budget

    def _manager(self, record, remaining, *, cleanup=False):
        deadline = time.monotonic() + remaining
        if (set(record) != {'state', 'scope', 'directory', 'roots', 'ids', 'assets', 'configuration', 'fingerprints', 'directory_identity'}
                or record['state'] != 'ready' or not isinstance(record['scope'], str)
                or len(record['scope']) != 16 or any(c not in '0123456789abcdef' for c in record['scope'])
                or not isinstance(record['ids'], list) or len(record['ids']) != 1
                or not isinstance(record['ids'][0], str) or len(record['ids'][0]) != 64
                or any(c not in '0123456789abcdef' for c in record['ids'][0])):
            raise PrerequisiteError('Reporting binding is pending or invalid; verified recovery is required.')
        if record['configuration'] != _configuration(self.plan):
            raise PrerequisiteError('Shared reporting bucket, region or assets differ; no initialization was performed.')
        directory, scope = _binding_location(record, self.key, self.directory)
        plan = rebind_reporting_plan(self.plan, scope)
        manager = self.factory(plan, scope, directory, engine_environment=self.environment,
                               cancelled=self.cancelled, deadline_seconds=remaining)
        if record['assets'] != _assets(plan):
            if manager.fingerprints != record['fingerprints']:
                raise PrerequisiteError('Selected LocalStack configuration changed; no reuse was performed.')
            retained = _retained_assets(manager, record, directory, deadline,
                                        (lambda: False) if cleanup else self.cancelled, cleanup=cleanup)
            plan = replace(plan, assets=MappingProxyType(retained))
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (not cleanup and self.cancelled()):
                raise PrerequisiteError('Retained reporting verification was cancelled or timed out.')
            recovered = self.factory(plan, scope, directory, engine_environment=self.environment,
                                     cancelled=self.cancelled, deadline_seconds=remaining)
            if recovered.specs['localstack'] != manager.specs['localstack']:
                raise PrerequisiteError('Retained LocalStack service differs from the reviewed configuration.')
            manager = recovered
        if manager.fingerprints != record['fingerprints']:
            raise PrerequisiteError('Shared reporting configuration changed; no initialization was performed.')
        return manager

    def _pending(self):
        if self.pending_cleanup is None:
            from api_agent.providers.control import CleanupPendingError
            from .operation import OperationCleanup
            cleanup = OperationCleanup(self, None, None, self, False, self.timeout)
            self.pending_cleanup = CleanupPendingError((self,), cleanup.retry)
        return self.pending_cleanup

    def _original_lock(self, record, budget, *, cleanup=False):
        try:
            directory, scope = _binding_location(record, self.key, self.directory)
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise PrerequisiteError('Invalid reporting binding; no ownership lock was opened.') from error
        return _locked(directory, scope,
                       deadline=time.monotonic() + budget.remaining,
                       cancelled=(lambda: False) if cleanup else self.cancelled,
                       now=time.monotonic, sleep=time.sleep)

    def acquire(self):
        if self.intent or (self.manager is not None and self.manager._rollback is not None):
            raise self._pending()
        try:
            return self._acquire()
        except BaseException as primary:
            if self.intent or (self.manager is not None and self.manager._rollback is not None):
                primary.cleanup_pending = self._pending()
            raise

    def _acquire(self):
        if self.lease is not None:
            return self.lease
        with self._binding() as (path, budget):
            record = _read(path)
            if record is not None:
                with self._original_lock(record, budget):
                    try:
                        self.manager = self._manager(record, budget.remaining)
                    except (KeyError, TypeError, ValueError, OSError) as error:
                        raise PrerequisiteError('Invalid reporting binding; no initialization was performed.') from error
                    self.ownership_identity = record['directory_identity']
                    return self.manager.acquire(expected_ids=tuple(record['ids']),
                        reporting_directory=Path(record['directory']), _prelocked=True,
                        _deadline=time.monotonic() + budget.remaining)
            if self.occupied(int(self.plan.environment['SA_LOCALSTACK_PORT'])):
                raise PrerequisiteError('Reporting endpoint is occupied and unregistered; use explicit verified enrollment or publication-only storage.')
            scope = StackScope(self.key)
            directory = _directory(path.parent / 'ownership', create=True)
            plan = rebind_reporting_plan(self.plan, scope)
            self.manager = self.factory(plan, scope, directory, engine_environment=self.environment,
                                        cancelled=self.cancelled, deadline_seconds=budget.remaining)
            record = dict(state='provisioning', scope=scope.id, directory=str(directory), roots=[], ids=[],
                          configuration=_configuration(plan), assets=_assets(plan), fingerprints=self.manager.fingerprints,
                          directory_identity=[directory.stat().st_dev, directory.stat().st_ino])
            # Durable intent precedes any engine mutation. An interrupted intent
            # refuses ordinary acquisition until explicitly verified/recovered.
            _write(path, record)
            self.intent = True
            self.ownership_identity = record['directory_identity']
            lease = self.manager.acquire(expected_ids=())
            record.update(state='ready', ids=list(lease.container_ids))
            _write(path, record)
            self.intent = False
            return lease

    def release(self, *, retain=False):
        if self.manager is None:
            return
        with self._binding(cleanup=True) as (path, budget):
            directory = _directory(self.manager.directory)
            info = directory.stat()
            if [info.st_dev, info.st_ino] != self.ownership_identity:
                raise PrerequisiteError('Retained reporting journal directory changed; cleanup remains owned.')
            record = _read(path)
            if not self.intent and self.manager._rollback is None:
                # Revalidate metadata before releasing the exact retained owner.
                with self._original_lock(record, budget, cleanup=True):
                    try:
                        self._manager(record, budget.remaining, cleanup=True)
                    except (KeyError, TypeError, ValueError, OSError) as error:
                        raise PrerequisiteError('Invalid reporting binding; cleanup remains owned.') from error
                    if record['scope'] != self.manager.scope.id or record['directory'] != str(directory):
                        raise PrerequisiteError('Reporting registration changed; cleanup remains owned.')
                    retained_ids = self.lease.container_ids if self.lease is not None else ()
                    if self.lease is not None and tuple(record['ids']) != tuple(retained_ids):
                        raise PrerequisiteError('Reporting container registration changed; cleanup remains owned.')
                    self.manager.release(retain=retain, reporting_ids=tuple(retained_ids),
                                         reporting_directory=directory, _prelocked=True,
                                         _deadline=time.monotonic() + budget.remaining)
            else:
                self.manager.release(retain=retain)
            if self.intent:
                if self.manager._inventory(time.monotonic() + budget.remaining, cleanup=True):
                    raise PrerequisiteError('Reporting provisioning cleanup remains pending.')
                path.unlink()
                self.intent = False
            self.pending_cleanup = None


def enroll_reporting(plan, *, base_root, pack_root, ownership_directory, **options):
    """Explicitly register a settled, operator-selected existing installation.

    Never discovers roots or journals from engine labels. This changes only local
    registry metadata; it neither initializes nor mutates the selected engine.
    """
    shared = SharedReportingManager(plan, **options)
    roots = [_directory(root) for root in (base_root, pack_root)]
    scope = StackScope.from_roots(*roots)
    original = _directory(ownership_directory)
    selected = rebind_reporting_plan(plan, scope)
    with shared._binding() as (path, budget):
        if _read(path) is not None:
            raise PrerequisiteError('Reporting endpoint is already registered.')
        with _locked(original, scope, deadline=time.monotonic() + budget.remaining,
                     cancelled=shared.cancelled, now=time.monotonic, sleep=time.sleep):
            if _read(original / (scope.id + '.json')) is None:
                raise PrerequisiteError('Enrollment requires the original ownership journal.')
            if _read_journal(original, scope, process_identity):
                raise PrerequisiteError('Enrollment requires settled original invocation leases.')
            manager = shared.factory(selected, scope, original, engine_environment=shared.environment,
                                     cancelled=shared.cancelled, deadline_seconds=budget.remaining)
            inventory = manager._inventory(time.monotonic() + budget.remaining)
            current = manager._compatible(inventory, time.monotonic() + budget.remaining)
            if set(current) != {'localstack'} or len(inventory) != 1:
                raise PrerequisiteError('Enrollment requires one verified running LocalStack installation.')
            item = current['localstack']
            manager.verify_reporting_storage(item.id, original, time.monotonic() + budget.remaining)
            record = dict(state='ready', scope=scope.id, directory=str(original), ids=[item.id],
                configuration=_configuration(selected), assets=_assets(selected), fingerprints=manager.fingerprints,
                directory_identity=[original.stat().st_dev, original.stat().st_ino],
                roots=[dict(path=str(root), identity=[root.stat().st_dev, root.stat().st_ino]) for root in roots])
            _write(path, record)
    return shared.key
