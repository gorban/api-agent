"""Owned prerequisite lifecycle with cross-process leases and bounded CLI calls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import selectors
import stat
import shutil
import shlex
import subprocess
import sys
import time
import tempfile
from types import MappingProxyType
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from api_agent.runtime.compose import controlled_engine_environment, snapshot_compose
from api_agent.testing.processes import disposal_complete, finalize

CONFIG_LABEL = 'api-agent.config'
_ID = re.compile(r'[0-9a-f]{64}')


class PrerequisiteError(RuntimeError):
    """Safe public message with optional bounded evidence for local inspection."""

    def __init__(self, message, *, local_details=""):
        super().__init__(message)
        self.local_details = local_details


class PrerequisiteCancelled(PrerequisiteError):
    pass


@dataclass(frozen=True)
class ServiceLease:
    invocation_id: str
    scope_id: str
    profiles: tuple[str, ...]
    owned_ids: tuple[str, ...]
    reused_ids: tuple[str, ...]

    @property
    def container_ids(self):
        return self.owned_ids + self.reused_ids


@dataclass(frozen=True)
class ManagedService:
    id: str
    service: str
    image: str
    image_id: str | None
    fingerprint: str
    running: bool
    health: str | None
    profile: str


def process_identity(pid):
    """PID birth time plus host boot identity; uncertainty never means stale."""
    if Path('/proc/sys/kernel/random/boot_id').is_file():
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError as error:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            except OSError as probe_error:
                raise PrerequisiteError('Cannot verify lease process existence.') from probe_error
            raise PrerequisiteError('Cannot verify lease process identity.') from error
        except (OSError, IndexError) as error:
            raise PrerequisiteError('Cannot verify lease process identity.') from error
        if not fields or len(fields) <= 19 or not fields[19].isdigit():
            raise PrerequisiteError('Cannot verify lease process birth time.')
        if fields[0] == 'Z':
            return None
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError as error:
            raise PrerequisiteError('Cannot verify host boot identity.') from error
        if not boot:
            raise PrerequisiteError('Cannot verify host boot identity.')
        return boot + ':' + fields[19]
    if sys.platform != 'darwin':
        raise PrerequisiteError('Cannot verify lease process identity on this platform.')
    try:
        options = dict(capture_output=True, text=True, timeout=1, check=False,
                       env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], **options)
        birth = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'stat='], **options)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrerequisiteError('Cannot verify lease process identity.') from error
    if boot.returncode or not boot.stdout.strip():
        raise PrerequisiteError('Cannot verify host boot identity.')
    if birth.returncode == 1 and not birth.stdout.strip() and not birth.stderr.strip():
        return None
    if birth.returncode or not birth.stdout.strip():
        raise PrerequisiteError('Cannot verify lease process identity.')
    if birth.stdout.split()[-1].startswith('Z'):
        return None
    return boot.stdout.strip() + ':' + birth.stdout.rsplit(None, 1)[0]


def _check(deadline, cancelled, now):
    if cancelled():
        raise PrerequisiteCancelled('Prerequisite operation cancelled.')
    if now() >= deadline:
        raise PrerequisiteError('Prerequisite deadline exceeded.')



def run_command(command, *, environment, directory, deadline, cancelled,
                now=time.monotonic, combine_output=False, output_scrubber=None, output_limit=None):
    """Bound combined output while running; always reap the owned process group."""
    if output_limit is not None and (type(output_limit) is not int or output_limit < 256):
        raise ValueError('Invalid combined output byte limit')
    captured = {'stdout': bytearray(), 'stderr': bytearray()}
    totals = {'stdout': 0, 'stderr': 0}
    process = None

    def evidence():
        from api_agent.testing.diagnostics import CredentialScrubber, scrub_command
        original_length = len(shlex.join(str(part) for part in command))
        scrubber = output_scrubber if output_scrubber is not None else CredentialScrubber(environment)
        safe_arguments = tuple(scrubber(str(part)) for part in command)
        command_text = shlex.join(scrub_command(safe_arguments, environment))
        omitted = max(0, original_length - 4096)
        command_text = command_text[:4096]
        if omitted:
            command_text += f' [truncated: {omitted} command characters omitted]'
        status = process.returncode if process is not None else None
        lines = [f'Command: {command_text}', f'Exit code: {status if status is not None else "unavailable"}']
        for stream in ('stdout', 'stderr'):
            # Keep the complete bounded capture until scrubbing so a registered
            # value spanning reads or the tail boundary cannot expose its suffix.
            # Every failure capture may end mid-value (bound, cancellation,
            # deadline or cleanup). Protect prefixes before decoding or slicing.
            safe = scrubber.capture(captured[stream]).encode('utf-8')
            tail = safe[-16384:].decode('utf-8', errors='ignore')
            omitted = max(0, len(safe) - len(tail.encode('utf-8')))
            lines.append(f'{stream} ({totals[stream]} bytes captured; {omitted} bytes omitted):')
            lines.append(tail or '(no output captured)')
        return '\n'.join(lines)

    try:
        _check(deadline, cancelled, now)
    except PrerequisiteError as error:
        error.local_details = evidence()
        raise
    try:
        process = subprocess.Popen(command, cwd=directory, env=dict(environment),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        raise PrerequisiteError('Prerequisite command unavailable.', local_details=evidence()) from error
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    stdout, size = [], 0
    primary_error = None
    try:
        while selector.get_map() or process.poll() is None:
            _check(deadline, cancelled, now)
            timeout = min(.1, max(.001, deadline - now()))
            if not selector.get_map():
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    continue
            for key, _ in selector.select(timeout):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                totals[key.data] += len(chunk)
                captured[key.data].extend(chunk)
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise PrerequisiteError('Prerequisite command response exceeds the safe bound.')
                if key.data == 'stdout':
                    stdout.append(chunk)
        if process.returncode:
            raise PrerequisiteError(f'Prerequisite command failed (exit {process.returncode}).')
        if combine_output:
            from api_agent.testing.diagnostics import CredentialScrubber
            scrubber = output_scrubber if output_scrubber is not None else CredentialScrubber(environment)
            safe_streams = {stream: scrubber.capture(captured[stream]) for stream in ('stdout', 'stderr')}
            if output_limit is not None:
                # Keep both origins visible even when a single line is huge.
                # Scrubbing must precede every cut, including UTF-8 boundaries.
                allowance = (output_limit - len('stdout:\n\n\nstderr:\n')) // 2
                marker = '[Earlier output omitted]\n'
                for stream, text in safe_streams.items():
                    data = text.encode('utf-8')
                    if len(data) > allowance:
                        safe_streams[stream] = marker + data[-(allowance - len(marker)):].decode('utf-8', errors='ignore')
            return '\n\n'.join(f'{stream}:\n{safe_streams[stream]}' for stream in ('stdout', 'stderr'))
        return b''.join(stdout).decode('utf-8', errors='replace')
    except BaseException as error:
        primary_error = error
        error.local_details = evidence()
        raise
    finally:
        selector.close()
        # Close captured pipes before TERM/KILL so cleanup cannot accumulate a
        # hostile writer's output during its termination grace period.
        process.stdout.close()
        process.stderr.close()
        try:
            finalize(process)
        except BaseException as error:
            cleanup = PrerequisiteError('Prerequisite process group cleanup failed.', local_details=evidence())
            cleanup.__cause__ = error
            interruption = (primary_error if primary_error is not None and not isinstance(primary_error, Exception)
                else error if not isinstance(error, Exception) else None)
            target = interruption if interruption is not None else cleanup
            if interruption is not None:
                prior_causes = (primary_error,) if primary_error is not None and primary_error is not interruption else ()
                interruption.diagnostic_errors = (*getattr(interruption, 'diagnostic_errors', ()), *prior_causes, cleanup)
            target.local_details = evidence()
            from api_agent.providers.control import CleanupPendingError
            from api_agent.testing.suite_lock import _cleanup_receipts
            duties = {}
            successor_chains = {}
            process_pending = not disposal_complete(process)
            def add_duties(failure):
                added = []
                # Aggregate diagnostic attachment must not erase the original
                # successor chain used to decide whether a duty was replaced.
                if id(failure) not in successor_chains or getattr(failure, 'cleanup_pending', None) is not receipt:
                    successor_chains[id(failure)] = (failure, tuple(_cleanup_receipts(failure)))
                for duty in successor_chains[id(failure)][1]:
                    if duty is not receipt:
                        if id(duty) not in duties:
                            duties[id(duty)] = [duty, True]
                        added.append(duty)
                receipt.receipts = tuple(state[0] for state in duties.values())
                return added
            def update_owners():
                owners = [owner for duty, pending in duties.values() if pending for owner in duty.owners]
                if process_pending:
                    owners.append(process)
                receipt.owners = tuple({id(owner): owner for owner in owners}.values())
            retry_lock = asyncio.Lock()
            async def retry_cleanup():
                # Cancellation waits for owned offloads to settle before this
                # lock admits a successor retry of the same receipt.
                try:
                    async with retry_lock:
                        await retry_owned_cleanup()
                except BaseException as retry_error:
                    retry_error.cleanup_pending = receipt
                    raise
            async def retry_owned_cleanup():
                from api_agent.runtime.tasks import offload
                nonlocal process_pending
                failures = []
                for state in tuple(duties.values()):
                    duty, pending = state
                    if not pending:
                        continue
                    try:
                        await duty.retry()
                        state[1] = False
                    except BaseException as retry_error:
                        successors = add_duties(retry_error)
                        if successors and all(successor is not duty for successor in successors):
                            state[1] = False
                        retry_error.cleanup_pending = receipt
                        failures.append(retry_error)
                if process_pending and disposal_complete(process):
                    process_pending = False
                if process_pending:
                    try:
                        await offload(finalize, process)
                        process_pending = False
                    except BaseException as retry_error:
                        successors = add_duties(retry_error)
                        if disposal_complete(process) or successors:
                            process_pending = False
                        retry_error.cleanup_pending = receipt
                        failures.append(retry_error)
                update_owners()
                if failures:
                    selected = next((failure for failure in failures if not isinstance(failure, Exception)), failures[0])
                    secondary = tuple(failure for failure in failures if failure is not selected)
                    selected.diagnostic_errors = (*getattr(selected, 'diagnostic_errors', ()), *secondary)
                    raise selected
            receipt = CleanupPendingError((), retry_cleanup)
            add_duties(primary_error)
            add_duties(error)
            update_owners()
            target.cleanup_pending = receipt
            raise target
        finally:
            if primary_error is not None:
                primary_error.local_details = evidence()


@contextmanager
def _locked(directory, scope, *, deadline, cancelled, now, sleep):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{scope.id}.lock'
    if path.is_symlink():
        raise PrerequisiteError('Ownership lock may not be a symlink.')
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        raise PrerequisiteError('Cannot open ownership lock.') from error
    with os.fdopen(descriptor, 'a') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PrerequisiteError('Ownership lock must be a regular user-owned file.')
        while True:
            _check(deadline, cancelled, now)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read_journal(directory, scope, identity):
    path = directory / f'{scope.id}.json'
    if path.is_symlink():
        raise PrerequisiteError('Ownership journal may not be a symlink.')
    try:
        from .reporting_metadata import _read
        journal = _read(path)
        if journal is None:
            return {}
    except (OSError, ValueError) as error:
        raise PrerequisiteError('Cannot read ownership journal.') from error
    active = {}
    for key, record in journal.items():
        if (not isinstance(record, dict) or not isinstance(record.get('pid'), int)
                or not isinstance(record.get('identity'), str)
                or not isinstance(record.get('ids'), list)
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in record['ids'])):
            raise PrerequisiteError('Invalid ownership lease record.')
        if identity(record['pid']) == record['identity']:
            active[key] = record
    return active


def _write_journal(directory, scope, journal):
    from .reporting_metadata import LIMIT
    payload = json.dumps(journal, sort_keys=True)
    if len(payload.encode()) > LIMIT:
        raise PrerequisiteError('Ownership journal exceeds its bounded size; existing leases are preserved.')
    path = directory / f'{scope.id}.json'
    temporary = directory / f'{scope.id}.{uuid4().hex}.tmp'
    try:
        with temporary.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as error:
        raise PrerequisiteError('Cannot persist ownership journal.') from error


@contextmanager
def guarded_cleanup(ownership_directory, scope, *, identity=process_identity,
                    deadline_seconds=15):
    """Hold the acquisition lock throughout caller's final lease check and stop.

    Yield active immutable IDs. The caller must refuse any intersection with its
    stop selection, and perform its ownership reinspection and stop inside this
    context. An uncertain journal/process identity raises and refuses cleanup.
    """
    directory = Path(ownership_directory).resolve()
    with _locked(directory, scope, deadline=time.monotonic() + deadline_seconds,
                 cancelled=lambda: False, now=time.monotonic, sleep=time.sleep):
        journal = _read_journal(directory, scope, identity)
        yield frozenset(item for record in journal.values() for item in record['ids'])


def active_leased_ids(ownership_directory, scope, *, identity=process_identity,
                      deadline_seconds=5):
    """Read-only snapshot; use guarded_cleanup for an atomic check and stop."""
    with guarded_cleanup(ownership_directory, scope, identity=identity,
                         deadline_seconds=deadline_seconds) as identifiers:
        return identifiers


def _interpolate(value, environment):
    if isinstance(value, dict):
        return {key: _interpolate(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, environment) for item in value]
    if isinstance(value, str):
        for key, item in environment.items():
            value = value.replace('${' + key + ':?controlled local development value required}', item)
    return value


class _NoReadinessRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise URLError('Local prerequisite readiness may not redirect.')


# Fixed loopback readiness must not inherit proxy settings or send API-key
# headers to a redirect destination. The injectable callable remains testable.
urlopen = build_opener(ProxyHandler({}), _NoReadinessRedirect()).open


def _readiness(profile, timeout, environment):
    url = {'langfuse': 'http://127.0.0.1:3000/api/public/projects',
           'otel': 'http://127.0.0.1:4318/v1/traces',
           'localstack': 'http://127.0.0.1:' + environment.get('SA_LOCALSTACK_PORT', '4566') + '/_localstack/health'}[profile]
    headers = {}
    data = None
    if profile == 'langfuse':
        auth = environment['SA_LANGFUSE_PUBLIC_KEY'] + ':' + environment['SA_LANGFUSE_SECRET_KEY']
        headers['Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()
    if profile == 'otel':
        headers['Content-Type'] = 'application/json'
        data = b'{"resourceSpans":[]}'
    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            if profile == 'langfuse':
                data = json.loads(response.read(65536))
                return any(project.get('id') == 'api-agent-' + environment['SA_SCOPE']
                           for project in data.get('data', []))
            if profile == 'localstack':
                data = json.loads(response.read(65536))
                if data.get('services', {}).get('s3') not in {'running', 'available'}:
                    return False
                # ready.d runs after S3 begins serving. Wait for the controlled
                # hook to finish before an idempotent exec, avoiding a create race.
                with urlopen(Request('http://127.0.0.1:' + environment.get('SA_LOCALSTACK_PORT', '4566') + '/_localstack/init/ready'), timeout=timeout) as ready:
                    if ready.status != 200:
                        return False
                    stage = json.loads(ready.read(65536))
                return (stage.get('completed') is True and any(
                    script.get('name') == '10-api-agent-s3.sh' and script.get('state') == 'SUCCESSFUL'
                    for script in stage.get('scripts', [])))
            return True
    except (OSError, URLError, ValueError, AttributeError, TypeError):
        return False


class PrerequisiteManager:
    def __init__(self, plan, scope, ownership_directory, *, engine_environment=None,
                 command_runner=run_command, cancelled=lambda: False,
                 readiness_probe=_readiness, deadline_seconds=120,
                 now=time.monotonic, sleep=time.sleep, identity=process_identity):
        self.plan, self.scope = plan, scope
        self._rollback = None
        if plan.environment.get('SA_SCOPE') != scope.id:
            raise PrerequisiteError('Prerequisite plan ownership does not match scope.')
        if deadline_seconds <= 0:
            raise PrerequisiteError('Prerequisite deadline must be positive.')
        self.directory = Path(ownership_directory).resolve()
        self.environment = MappingProxyType({**dict(plan.environment),
            **controlled_engine_environment(engine_environment)})
        self.runner, self.cancelled, self.probe = command_runner, cancelled, readiness_probe
        self.timeout, self.now, self.sleep, self.identity = deadline_seconds, now, sleep, identity
        document = json.loads(plan.assets['compose.json'])
        self.specs = {name: _interpolate(document['services'][name], self.environment) for name in plan.services}
        fingerprint_environment = {**self.environment,
            'SA_LOCALSTACK_READY_HOOK': 'sha256:' + self.environment['SA_LOCALSTACK_READY_DIGEST'],
            'SA_JAEGER_CONFIG': 'sha256:' + sha256(plan.assets['jaeger-config.json']).hexdigest()}
        self.fingerprints = {name: sha256(json.dumps(_interpolate(document['services'][name],
            fingerprint_environment), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                             for name, spec in self.specs.items()}
        self.lease = None
        self.snapshot_directory = None

    def _command(self, args, deadline, *, cleanup=False):
        return self.runner(tuple(args), environment=self.environment, directory=self.plan.directory,
            deadline=deadline, cancelled=(lambda: False) if cleanup else self.cancelled, now=self.now)

    def _inventory(self, deadline, *, cleanup=False):
        executable = self.plan.command[0]
        output = self._command((executable, 'ps', '-a', '--no-trunc', '--filter',
            f'label=com.docker.compose.project={self.scope.project}', '--format', '{{.ID}}'), deadline, cleanup=cleanup)
        for legacy in self.scope.legacy_ids:
            old = self._command((executable, 'ps', '-a', '--no-trunc', '--filter',
                f'label=com.docker.compose.project=sa-{legacy}', '--format', '{{.ID}}'), deadline, cleanup=cleanup)
            old_ids = old.split()
            if any(not _ID.fullmatch(item) for item in old_ids):
                raise PrerequisiteError('Invalid prerequisite container identifiers.')
            if old_ids:
                try:
                    records = json.loads(self._command((executable, 'inspect', *old_ids), deadline, cleanup=cleanup))
                    if not isinstance(records, list) or len(records) != len(old_ids):
                        raise ValueError()
                    legacy_present = any(self.scope.is_legacy(record['Config'].get('Labels')) for record in records)
                except (KeyError, TypeError, ValueError) as error:
                    raise PrerequisiteError('Invalid prerequisite inspection response.') from error
                if legacy_present:
                    raise PrerequisiteError(f'Previous path-based stack scope sa-{legacy} exists; review and release its leases and containers before migration.')
        ids = tuple(line.strip() for line in output.splitlines() if line.strip())
        if any(not _ID.fullmatch(item) for item in ids):
            raise PrerequisiteError('Invalid prerequisite container identifiers.')
        if not ids:
            return {}
        raw = self._command((executable, 'inspect', *ids), deadline, cleanup=cleanup)
        try:
            records = json.loads(raw)
            if not isinstance(records, list) or len(records) != len(ids):
                raise ValueError()
            result = {}
            for record in records:
                identifier = record['Id']
                config, state = record['Config'], record['State']
                labels = config.get('Labels', {})
                if identifier not in ids or identifier in result:
                    raise ValueError()
                if not self.scope.owns(labels):
                    raise PrerequisiteError('Compose project includes a container with foreign ownership.')
                result[identifier] = ManagedService(identifier, labels['com.docker.compose.service'],
                    config['Image'], record.get('Image'), labels.get(CONFIG_LABEL, ''), bool(state.get('Running')),
                    state.get('Health', {}).get('Status'), labels['api-agent.profile'])
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise PrerequisiteError('Invalid prerequisite inspection response.') from error

    def verify_reporting_storage(self, identifier, directory, deadline, *, cleanup=False):
        """Verify the retained controlled bind and named volume before adoption."""
        from .reporting_metadata import _directory
        try:
            records = json.loads(self._command((self.plan.command[0], 'inspect', identifier), deadline, cleanup=cleanup))
            record, = records
            if record['Id'] != identifier:
                raise ValueError()
            mounts = record['Mounts']
            hook, = [item for item in mounts if item['Destination'] == '/etc/localstack/init/ready.d/10-api-agent-s3.sh']
            source = Path(hook['Source'])
            parent = _directory(source.parent)
            if hook['Type'] != 'bind' or hook.get('RW') is not False or not parent.is_relative_to(directory):
                raise ValueError()
            for name, expected in self.plan.assets.items():
                path = parent / name
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_mode & 0o022 or info.st_size > len(expected)
                            or stream.read(len(expected) + 1) != expected):
                        raise ValueError()
            if source.name != 'localstack-ready.sh':
                raise ValueError()
            source, destination = next(value.split(':')[:2] for value in
                self.specs['localstack']['volumes'] if value.startswith('localstack-data:'))
            name = self.scope.project + '_' + source
            volume, = [item for item in mounts if item.get('Name') == name]
            if volume['Type'] != 'volume' or volume['Destination'] != destination:
                raise ValueError()
            volumes = json.loads(self._command((self.plan.command[0], 'volume', 'inspect', name), deadline, cleanup=cleanup))
            actual, = volumes
            labels = actual['Labels']
            if actual['Name'] != name or any(labels.get(key) != value for key, value in self.scope.labels('localstack').items()):
                raise ValueError()
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            raise PrerequisiteError('Reporting storage ownership or captured assets could not be verified.') from error

    def verify_jaeger_storage(self, identifier, deadline, *, cleanup=False):
        """Adopt only the owned immutable config bind and scoped Badger volume."""
        try:
            records = json.loads(self._command((self.plan.command[0], 'inspect', identifier), deadline,
                                               cleanup=cleanup))
            record, = records
            if record['Id'] != identifier:
                raise ValueError()
            mounts = record['Mounts']
            if (not isinstance(mounts, list) or len(mounts) != 2
                    or {item['Destination'] for item in mounts} != {'/etc/jaeger/config.json', '/tmp'}):
                raise ValueError()
            bind, = [item for item in mounts if item['Destination'] == '/etc/jaeger/config.json']
            source = Path(bind['Source'])
            parent = source.parent
            if (bind['Type'] != 'bind' or bind.get('RW') is not False
                    or source.name != 'jaeger-config.json' or not parent.is_relative_to(self.directory)):
                raise ValueError()
            inside_count = len(parent.relative_to(self.directory).parts) + 1
            for directory in (parent, *parent.parents)[:inside_count]:
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022):
                    raise ValueError()
            for name, expected in self.plan.assets.items():
                path = parent / name
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_mode & 0o022 or info.st_size > len(expected)
                            or stream.read(len(expected) + 1) != expected):
                        raise ValueError()
            volume, = [item for item in mounts if item['Destination'] == '/tmp']
            name = self.scope.project + '_jaeger-badger'
            if (volume['Type'] != 'volume' or volume.get('Name') != name
                    or volume.get('RW') is not True):
                raise ValueError()
            actual, = json.loads(self._command((self.plan.command[0], 'volume', 'inspect', name), deadline,
                                               cleanup=cleanup))
            if (actual['Name'] != name or any(actual['Labels'].get(key) != value
                    for key, value in self.scope.labels('otel').items())):
                raise ValueError()
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            raise PrerequisiteError('Jaeger storage ownership or captured assets could not be verified.') from error

    def _verified_digest_image(self, expected, actual_id, deadline, *, cleanup=False):
        """Verify a container's immutable image against the locally resolved pin."""
        if not isinstance(actual_id, str) or not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', actual_id):
            return False
        try:
            inspected = json.loads(self._command((self.plan.command[0], 'image', 'inspect', expected), deadline, cleanup=cleanup))
            if not isinstance(inspected, list) or len(inspected) != 1:
                return False
            image = inspected[0]
            repository, digest = expected.rsplit('@', 1)
            if repository.rfind(':') > repository.rfind('/'):
                repository = repository[:repository.rfind(':')]
            accepted = {f'{repository}@{digest}'}
            if repository.startswith('docker.io/'):
                familiar = repository[len('docker.io/'):]
                if familiar.startswith('library/') and '/' not in familiar[len('library/'):]:
                    familiar = familiar[len('library/'):]
                accepted.add(f'{familiar}@{digest}')
            repo_digests = image.get('RepoDigests')
            canonical_id = actual_id.removeprefix('sha256:')
            return (image.get('Id') in {canonical_id, 'sha256:' + canonical_id} and isinstance(repo_digests, list)
                    and any(value in accepted for value in repo_digests))
        except (ValueError, TypeError, AttributeError, KeyError):
            return False

    def _compatible(self, inventory, deadline, *, allow_stopped=False, cleanup=False):
        desired = {}
        verified = {}
        for item in inventory.values():
            if item.service not in self.specs:
                continue
            expected = self.specs[item.service]['image']
            if '@sha256:' in expected:
                if not isinstance(item.image_id, str):
                    image_matches = False
                else:
                    image_key = (expected, item.image_id)
                    if image_key not in verified:
                        verified[image_key] = self._verified_digest_image(expected, item.image_id, deadline, cleanup=cleanup)
                    # Engines may normalize Config.Image (including adding :latest).
                    # The running image ID and the requested repo digest are proof.
                    image_matches = verified[image_key]
            else:
                image_matches = item.image == expected
            if (item.service in desired or not image_matches
                    or item.fingerprint != self.fingerprints[item.service] or (not item.running and not allow_stopped)
                    or item.profile != self.specs[item.service]['profiles'][0]):
                raise PrerequisiteError('Existing prerequisite stack is incompatible; review it before replacement.')
            desired[item.service] = item
        return desired

    def _stop_new(self, identifiers, journal, deadline):
        protected = {item for record in journal.values() for item in record['ids']}
        eligible = set(identifiers) - protected
        if not eligible:
            return ()
        current = self._inventory(deadline, cleanup=True)
        owned = tuple(sorted(item for item in eligible if item in current))
        if owned:
            self._command((self.plan.command[0], 'stop', '--time', '5', *owned), deadline, cleanup=True)
        return owned

    def _rollback_acquisition(self, journal, deadline):
        newly, snapshot, invocation = self._rollback
        if newly is None:
            raise PrerequisiteError('Initial rollback ownership could not be verified; review the failed acquisition before cleanup.')
        journal = dict(journal)
        journal.pop(invocation, None)
        stopped = self._stop_new(newly, journal, deadline)
        _write_journal(self.directory, self.scope, journal)
        if snapshot is not None and not newly:
            shutil.rmtree(snapshot)
            self.snapshot_directory = None
        self._rollback = None
        return stopped

    def acquire(self, *, expected_ids=None, reporting_directory=None):
        if self._rollback is not None:
            raise PrerequisiteError('Prerequisite rollback remains pending; release before acquiring again.')
        if self.lease is not None:
            return self.lease
        deadline = self.now() + self.timeout
        with _locked(self.directory, self.scope, deadline=deadline, cancelled=self.cancelled,
                     now=self.now, sleep=self.sleep):
            for legacy in self.scope.legacy_ids:
                if (self.directory / f'{legacy}.json').exists():
                    raise PrerequisiteError(f'Previous path-based ownership journal {legacy}.json exists; review and release its leases before migration.')
            journal = _read_journal(self.directory, self.scope, self.identity)
            invocation = uuid4().hex
            pid = os.getpid()
            identity = self.identity(pid)
            if not identity:
                raise PrerequisiteError('Cannot establish invocation process identity.')
            if not self.plan.enabled:
                self.lease = ServiceLease(invocation, self.scope.id, (), (), ())
                return self.lease
            initial = self._inventory(deadline)
            if expected_ids is not None and (set(initial) != set(expected_ids)
                    or (reporting_directory is None and any(not item.running for item in initial.values()))):
                raise PrerequisiteError('Registered reporting container changed or stopped; no restart was attempted.')
            compatible = self._compatible(initial, deadline, allow_stopped=True)
            if 'jaeger' in compatible:
                self.verify_jaeger_storage(compatible['jaeger'].id, deadline)
            if reporting_directory is not None:
                if set(compatible) != {'localstack'}:
                    raise PrerequisiteError('Registered reporting service changed; no restart was attempted.')
                for identifier in expected_ids:
                    self.verify_reporting_storage(identifier, reporting_directory, deadline)
            existing = {name: item for name, item in compatible.items() if item.running}
            restarted = {item.id for item in compatible.values() if not item.running}
            protected = {item for record in journal.values() for item in record['ids']}
            if restarted & protected:
                raise PrerequisiteError('Stopped prerequisite has an active invocation lease.')
            newly = ()
            snapshot = None
            missing = tuple(name for name in self.plan.services if name not in existing)
            try:
                if restarted and reporting_directory is not None:
                    # Restart only the registered immutable objects. Compose up
                    # would select a fresh snapshot rather than the retained bind.
                    self._command((self.plan.command[0], 'start', *sorted(restarted)), deadline)
                elif missing:
                    override = self.directory / f'{self.scope.id}-labels.json'
                    if override.is_symlink():
                        raise PrerequisiteError('Compose ownership override may not be a symlink.')
                    override.write_text(json.dumps({'services': {name: {'labels': {
                        CONFIG_LABEL: fingerprint}} for name, fingerprint in self.fingerprints.items()}}))
                    # Private snapshot paths remain available for retained bind
                    # mounts. No mutable source asset reaches Compose execution.
                    snapshot = Path(tempfile.mkdtemp(prefix=f'{self.scope.id}-assets-', dir=self.directory))
                    self.snapshot_directory = snapshot
                    command, environment = snapshot_compose(self.plan, snapshot)
                    self.environment = MappingProxyType({**self.environment,
                        'SA_LOCALSTACK_READY_HOOK': environment['SA_LOCALSTACK_READY_HOOK'],
                        'SA_JAEGER_CONFIG': environment['SA_JAEGER_CONFIG']})
                    self._command((*command, '-f', str(override), 'up', '-d',
                        '--no-recreate', *missing), deadline)
                while True:
                    _check(deadline, self.cancelled, self.now)
                    inventory = self._inventory(deadline)
                    newly = tuple(item for item in inventory if (item not in initial or item in restarted)
                        and inventory[item].service in missing)
                    current = self._compatible(inventory, deadline)
                    if any(name not in current or current[name].id != item.id
                           for name, item in existing.items()):
                        raise PrerequisiteError('Existing prerequisite changed during acquisition.')
                    if (len(current) == len(self.specs)
                            and all(item.health in {None, 'healthy'} for item in current.values())
                            and all(self.probe(profile, min(1, max(.001, deadline - self.now())),
                                    self.environment) for profile in self.plan.profiles)):
                        _check(deadline, self.cancelled, self.now)
                        if 'jaeger' in current:
                            self.verify_jaeger_storage(current['jaeger'].id, deadline)
                        if 'localstack' in current and reporting_directory is None:
                            self._command((self.plan.command[0], 'exec', current['localstack'].id,
                                '/bin/sh', '/etc/localstack/init/ready.d/10-api-agent-s3.sh'), deadline)
                            _check(deadline, self.cancelled, self.now)
                        break
                    self.sleep(.1)
                reused = tuple(item.id for item in existing.values())
                lease = ServiceLease(invocation, self.scope.id, self.plan.profiles, newly, reused)
                pending_journal = dict(journal)
                pending_journal[invocation] = {'pid': pid, 'identity': identity, 'ids': list(lease.container_ids),
                    'owned_ids': list(newly), 'reused_ids': list(reused), 'fingerprints': self.fingerprints}
                _write_journal(self.directory, self.scope, pending_journal)
                self.lease = lease
                return lease
            except BaseException as primary:
                # Preserve the acquisition boundary before any fallible cleanup.
                # A failed up can create containers before returning an error.
                self._rollback = (None, snapshot, invocation)
                try:
                    rollback_deadline = self.now() + 15
                    inventory = self._inventory(rollback_deadline, cleanup=True)
                    newly = tuple(item for item in inventory if (item not in initial or item in restarted)
                        and inventory[item].service in missing)
                    # Pin IDs while the acquisition lock still excludes other
                    # invocations; a retry cannot adopt later replacements.
                    self._rollback = (newly, snapshot, invocation)
                    self._rollback_acquisition(journal, rollback_deadline)
                except BaseException:
                    from api_agent.providers.control import CleanupPendingError
                    primary.cleanup_pending = CleanupPendingError((self,), self.retry_rollback)
                raise

    async def retry_rollback(self):
        from api_agent.runtime.tasks import offload
        await offload(self.release, retain=False)

    def release(self, *, retain=False, reporting_ids=None, reporting_directory=None):
        if self.lease is None and self._rollback is None:
            return ()
        deadline = self.now() + 15
        with _locked(self.directory, self.scope, deadline=deadline, cancelled=lambda: False,
                     now=self.now, sleep=self.sleep):
            if self.lease is None and self._rollback is None:
                return ()
            journal = _read_journal(self.directory, self.scope, self.identity)
            if self._rollback is not None:
                return self._rollback_acquisition(journal, deadline)
            journal.pop(self.lease.invocation_id, None)
            if reporting_ids is not None and not retain:
                # The final consumer may have reused every object. Ownership is
                # the verified registry chain, not which invocation created it.
                current = self._inventory(deadline, cleanup=True)
                if set(current) != set(reporting_ids):
                    raise PrerequisiteError('Registered reporting container changed; cleanup remains owned.')
                compatible = self._compatible(current, deadline, allow_stopped=True, cleanup=True)
                if set(compatible) != {'localstack'}:
                    raise PrerequisiteError('Registered reporting service changed; cleanup remains owned.')
                for identifier in reporting_ids:
                    self.verify_reporting_storage(identifier, reporting_directory, deadline, cleanup=True)
                protected = {item for record in journal.values() for item in record['ids']}
                eligible = tuple(sorted(item for item in reporting_ids
                    if item not in protected and current[item].running))
                if eligible:
                    self._command((self.plan.command[0], 'stop', '--time', '5', *eligible), deadline, cleanup=True)
                stopped = eligible
            else:
                stopped = () if retain else self._stop_new(self.lease.owned_ids, journal, deadline)
            _write_journal(self.directory, self.scope, journal)
            # Both running and stopped container definitions keep their original
            # bind source. Removal/asset garbage collection requires an explicit
            # inventory of those definitions, beyond this stop-only lifecycle.
            # Forget local cleanup ownership after the lease is relinquished.
            self.snapshot_directory = None
            self.lease = None
            return stopped
