"""Owned prerequisite lifecycle with cross-process leases and bounded CLI calls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from http.client import HTTPConnection, HTTPException, HTTPResponse
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import shutil
import shlex
import subprocess
import sys
import time
import tempfile
from types import MappingProxyType, SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from api_agent.runtime.compose import ENGINE_ENVIRONMENT_KEYS, controlled_engine_environment, snapshot_compose
from api_agent.runtime.ownership import PROFILES
from api_agent.runtime.processes import disposal_complete, finalize, list_processes, observe_exit, wait_owned

CONFIG_LABEL = 'api-agent.config'
_ID = re.compile(r'[0-9a-f]{64}')


class PrerequisiteError(RuntimeError):
    """Safe public message with optional bounded evidence for local inspection."""

    def __init__(self, message, *, local_details=""):
        super().__init__(message)
        self.local_details = local_details


class MissingCapturedAssetsError(PrerequisiteError):
    """Verified retained containers need their deleted host captures recreated."""


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
    initialization_changes: tuple[str, ...] = ()


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
        boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True, text=True,
                              timeout=1, check=False, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        birth = list_processes('started', 'state', pid=pid, timeout=1)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise PrerequisiteError('Cannot verify lease process identity.') from error
    if boot.returncode or not boot.stdout.strip():
        raise PrerequisiteError('Cannot verify host boot identity.')
    if not birth or birth[0][1].startswith('Z'):
        return None
    return boot.stdout.strip() + ':' + birth[0][0]


def _check(deadline, cancelled, now):
    if cancelled():
        raise PrerequisiteCancelled('Prerequisite operation cancelled.')
    if now() >= deadline:
        raise PrerequisiteError('Prerequisite deadline exceeded.')



def run_command(command, *, environment, directory, deadline, cancelled,
                now=time.monotonic, combine_output=False, output_scrubber=None, output_limit=None,
                inherited_fds=(), on_disposed=None, on_not_started=None):
    """Bound combined output while running; always reap the owned process group."""
    if (type(inherited_fds) is not tuple
            or any(type(fd) is not int or fd < 0 for fd in inherited_fds)
            or len(set(inherited_fds)) != len(inherited_fds)):
        raise ValueError('Invalid inherited prerequisite descriptors')
    for descriptor in inherited_fds:
        os.fstat(descriptor)
    if on_disposed is not None and not callable(on_disposed):
        raise ValueError('Invalid prerequisite disposal observer')
    if on_not_started is not None and not callable(on_not_started):
        raise ValueError('Invalid prerequisite no-start observer')
    if output_limit is not None and (type(output_limit) is not int or output_limit < 256):
        raise ValueError('Invalid combined output byte limit')
    captured = {'stdout': bytearray(), 'stderr': bytearray()}
    totals = {'stdout': 0, 'stderr': 0}
    process = None
    observed_exit_code = None

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
        if status is None:
            status = observed_exit_code
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
        # This check precedes Popen. Unlike process-exit observation, it proves
        # that no process group was ever reserved for this command.
        if on_not_started is not None:
            on_not_started()
        raise
    try:
        process = subprocess.Popen(command, cwd=directory, env=dict(environment),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            **({'pass_fds': inherited_fds} if inherited_fds else {}))
    except OSError as error:
        raise PrerequisiteError('Prerequisite command unavailable.', local_details=evidence()) from error
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    stdout, size = [], 0
    primary_error = None
    try:
        while selector.get_map() or observe_exit(process) is None:
            _check(deadline, cancelled, now)
            timeout = min(.1, max(.001, deadline - now()))
            if not selector.get_map():
                try:
                    wait_owned(process, timeout=timeout)
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
        exit_code = observed_exit_code = observe_exit(process)
        if exit_code:
            raise PrerequisiteError(f'Prerequisite command failed (exit {exit_code}).')
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
        raise
    finally:
        escaping_error = cleanup_diagnostic = None
        try:
            selector.close()
            # Close captured pipes before TERM/KILL so cleanup cannot accumulate a
            # hostile writer's output during its termination grace period.
            process.stdout.close()
            process.stderr.close()
            try:
                finalize(process)
            except BaseException as error:
                cleanup = PrerequisiteError('Prerequisite process group cleanup failed.')
                cleanup_diagnostic = cleanup
                cleanup.__cause__ = error
                interruption = (primary_error if primary_error is not None and not isinstance(primary_error, Exception)
                    else error if not isinstance(error, Exception) else None)
                target = interruption if interruption is not None else cleanup
                if interruption is not None:
                    prior_causes = (primary_error,) if primary_error is not None and primary_error is not interruption else ()
                    interruption.diagnostic_errors = (*getattr(interruption, 'diagnostic_errors', ()), *prior_causes, cleanup)
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
        except BaseException as error:
            escaping_error = error
            raise
        finally:
            failures = {id(error): error for error in (primary_error, escaping_error, cleanup_diagnostic)
                        if error is not None}
            if failures:
                details = evidence()
                for error in failures.values():
                    error.local_details = details
            if on_disposed is not None and disposal_complete(process):
                on_disposed()


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
    stale_duties = []
    for key, record in journal.items():
        if (not isinstance(record, dict) or not isinstance(record.get('pid'), int)
                or not isinstance(record.get('identity'), str)
                or not isinstance(record.get('ids'), list)
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in record['ids'])
                or ('conditional_engine_id' in record and (not isinstance(record['conditional_engine_id'], str)
                    or not _ID.fullmatch(record['conditional_engine_id'])))
                or not isinstance(record.get('owned_ids', []), list)
                or any(item not in record['ids'] for item in record.get('owned_ids', []))
                or type(record.get('cleanup_only', False)) is not bool
                or (record.get('cleanup_only', False) and not record.get('conditional_cleanup_ids'))
                or not isinstance(record.get('conditional_cleanup_ids', []), list)
                or any(item not in record['ids'] for item in record.get('conditional_cleanup_ids', []))):
            raise PrerequisiteError('Invalid ownership lease record.')
        record = dict(record)
        if record.get('conditional_engine_id') and record.get('owned_ids'):
            record['conditional_cleanup_ids'] = sorted(set(record.get('conditional_cleanup_ids', [])) |
                                                       set(record['owned_ids']))
        if not record.get('cleanup_only', False) and identity(record['pid']) == record['identity']:
            active[key] = dict(record)
        elif record.get('conditional_cleanup_ids'):
            stale_duties.append((key, record))
    for key, record in stale_duties:
        remaining = set(record['conditional_cleanup_ids'])
        for successor_key in sorted(active):
            successor = active[successor_key]
            if (successor.get('cleanup_only', False) or not record.get('conditional_engine_id')
                    or successor.get('conditional_engine_id') != record['conditional_engine_id']):
                continue
            transferred = remaining.intersection(successor['ids'])
            if transferred:
                successor['conditional_cleanup_ids'] = sorted(
                    set(successor.get('conditional_cleanup_ids', [])) | transferred)
                remaining -= transferred
        if remaining:
            active[key] = {**record, 'cleanup_only': True, 'ids': sorted(remaining),
                           'conditional_cleanup_ids': sorted(remaining), 'owned_ids': []}
    return active


def _leased_ids(journal):
    return {item for record in journal.values() if not record.get('cleanup_only', False)
            for item in record['ids']}


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
        yield frozenset(_leased_ids(journal))


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
            value = value.replace('${' + key + ':?controlled asset required}', item)
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


def _request_remaining(deadline, cancelled):
    if cancelled():
        raise PrerequisiteCancelled('Prerequisite operation cancelled.')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Prerequisite HTTP deadline exceeded.')
    return remaining


class _DeadlineHTTPReader(io.RawIOBase):
    def __init__(self, connection, deadline, cancelled):
        self.connection, self.deadline, self.cancelled = connection, deadline, cancelled

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            remaining = _request_remaining(self.deadline, self.cancelled)
            self.connection.settimeout(min(.05, remaining))
            try:
                return self.connection.recv_into(buffer)
            except TimeoutError:
                # Progress cannot renew the absolute request budget. Short idle
                # polls also let cancellation interrupt a quiet peer promptly.
                continue


@contextmanager
def _identity_response(request, timeout, *, cancelled=lambda: False):
    deadline = time.monotonic() + timeout
    parsed = urlsplit(request.full_url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'}
            or parsed.port is None or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Prerequisite identity requires a loopback HTTP endpoint.')
    remaining = _request_remaining(deadline, cancelled)
    with socket.create_connection(('127.0.0.1', parsed.port), timeout=min(.05, remaining)) as connection:
        connection.settimeout(min(.05, _request_remaining(deadline, cancelled)))
        client = HTTPConnection(parsed.hostname, parsed.port)
        client.sock = connection
        try:
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            client.request('GET', path, headers={'Connection': 'close'})
            reader = _DeadlineHTTPReader(connection, deadline, cancelled)
            response = HTTPResponse(SimpleNamespace(makefile=lambda mode: io.BufferedReader(reader)))
            try:
                response.begin()
                yield response
            finally:
                response.close()
        finally:
            client.close()


def _http_identity(readiness, timeout, *, cancelled=lambda: False):
    """Probe a captured loopback contract without proxies or redirects."""
    deadline = time.monotonic() + timeout
    try:
        with _identity_response(Request(readiness.url), timeout=timeout, cancelled=cancelled) as response:
            if response.status != readiness.status or 300 <= response.status < 400:
                return False
            body = response.read(65537)
            if len(body) > 65536 or getattr(response, 'length', None) not in (None, 0):
                return False
            if readiness.kind == 'http-status':
                _request_remaining(deadline, cancelled)
                return True
            value = json.loads(body)
            matches = (isinstance(value, dict)
                and type(value.get(readiness.identity_field)) is type(readiness.identity_value)
                and value.get(readiness.identity_field) == readiness.identity_value)
            _request_remaining(deadline, cancelled)
            return matches
    except (OSError, URLError, HTTPException, ValueError, AttributeError, TypeError):
        return False


class PrerequisiteManager:
    def __init__(self, plan, scope, ownership_directory, *, engine_environment=None,
                 command_runner=run_command, cancelled=lambda: False,
                 readiness_probe=_readiness, deadline_seconds=120,
                 now=time.monotonic, sleep=time.sleep, identity=process_identity,
                 cleanup_timeout_seconds=15, engine_target=None, engine_registry=None):
        self.plan, self.scope = plan, scope
        self.conditional = {item.compose_name: item for item in getattr(plan, 'conditional', ())}
        self._rollback = None
        if plan.environment.get('SA_SCOPE') != scope.id:
            raise PrerequisiteError('Prerequisite plan ownership does not match scope.')
        if deadline_seconds <= 0:
            raise PrerequisiteError('Prerequisite deadline must be positive.')
        self.directory = Path(ownership_directory).resolve()
        self.engine_target = engine_target
        if engine_target is not None:
            self.directory = self.directory / "engines" / engine_target.lease_key
            if plan.command[0] != engine_target.executable:
                raise PrerequisiteError("Prerequisite executable differs from selected engine.")
            if engine_environment is not None and dict(engine_environment) != dict(engine_target.environment):
                raise PrerequisiteError("Prerequisite environment differs from selected engine.")
            engine_environment = engine_target.environment
            engine_target.verify(cancelled=cancelled)
        engine_override = controlled_engine_environment(engine_environment)
        if self.conditional and any(plan.environment.get(key) != value
                for key, value in engine_override.items()):
            raise PrerequisiteError('Prerequisite engine configuration differs from the captured plan.')
        self.environment = MappingProxyType({**dict(plan.environment), **engine_override})
        self.engine_environment = MappingProxyType({key: self.environment[key]
            for key in ENGINE_ENVIRONMENT_KEYS if key in self.environment})
        self.engine_id = sha256(json.dumps({'command': plan.command[0],
            'environment': dict(self.engine_environment)}, sort_keys=True).encode()).hexdigest()
        if engine_target is not None:
            self.engine_id = engine_target.lease_key
        self.runner, self.cancelled, self.probe = command_runner, cancelled, readiness_probe
        if (type(cleanup_timeout_seconds) not in (int, float)
                or not math.isfinite(cleanup_timeout_seconds) or not 0 < cleanup_timeout_seconds <= 300):
            raise PrerequisiteError('Cleanup deadline must be between zero and 300 seconds.')
        self.cleanup_timeout = cleanup_timeout_seconds
        self.timeout, self.now, self.sleep, self.identity = deadline_seconds, now, sleep, identity
        document = json.loads(plan.assets['compose.json'])
        self.document = document
        self.inventory_pairs = frozenset((name, profile)
            for name, spec in document['services'].items() for profile in spec.get('profiles', ()))
        catalog = getattr(plan, 'inventory', None)
        if catalog is not None:
            from .inventory import InventoryCatalog
            if type(catalog) is not InventoryCatalog or catalog.scope_id != scope.id:
                raise PrerequisiteError('Inventory scope differs from prerequisite scope.')
            pairs = catalog.verify(scope)
            if any(name in document['services'] and profile not in document['services'][name].get('profiles', ())
                   for name, profile in pairs):
                raise PrerequisiteError('Inventory conflicts with selected service ownership.')
            self.inventory_pairs |= frozenset(pairs)
        self.specs = {name: _interpolate(document['services'][name], self.environment) for name in plan.services}
        if 'jaeger' in plan.services and 'jaeger-config.json' not in plan.assets:
            raise PrerequisiteError('Selected Jaeger configuration is missing.')
        fingerprint_environment = dict(self.environment)
        if 'SA_LOCALSTACK_READY_DIGEST' in self.environment:
            fingerprint_environment['SA_LOCALSTACK_READY_HOOK'] = 'sha256:' + self.environment['SA_LOCALSTACK_READY_DIGEST']
        for item in self.conditional.values():
            fingerprint_environment.update({key: 'sha256:' + digest for key, digest in item.asset_digests.items()})
        if 'jaeger-config.json' in plan.assets:
            fingerprint_environment['SA_JAEGER_CONFIG'] = 'sha256:' + sha256(plan.assets['jaeger-config.json']).hexdigest()
        self.fingerprints = {name: sha256(json.dumps(_interpolate(document['services'][name],
            fingerprint_environment), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                             for name, spec in self.specs.items()}
        self.lease = None
        self.snapshot_directory = None
        if engine_target is not None and engine_registry is not None:
            engine_registry.remember(engine_target)

    def _verify_engine(self, *, cleanup=False):
        if self.engine_target is not None:
            from .engine_targets import EngineTargetError
            try:
                self.engine_target.verify(cancelled=(lambda: False) if cleanup else self.cancelled)
            except EngineTargetError:
                raise PrerequisiteError("Selected engine unavailable or identity changed.") from None

    def _command(self, args, deadline, *, cleanup=False):
        # Serialize network creation and pruning across checkouts/scopes on this
        # user. Different PATH/context aliases can reach the same engine, so a
        # per-user lock is deliberately more conservative than engine_id here.
        # Age is creation time, not last use: even an old empty network
        # may be in the middle of being reattached by another Compose invocation.
        changes_networks = args[1] == 'compose' and 'up' in args
        lock = self._network_guard(deadline, cleanup=cleanup) if changes_networks else nullcontext()
        with lock:
            self._verify_engine(cleanup=cleanup)
            if args[1] == 'compose' and 'up' in args:
                self._prepare_network(deadline)
            return self.runner(tuple(args), environment=self.environment, directory=self.plan.directory,
                deadline=deadline, cancelled=(lambda: False) if cleanup else self.cancelled, now=self.now)

    def _network_guard(self, deadline, *, cleanup=False):
        return _locked(Path.home() / '.cache/api-agent/network-locks',
            SimpleNamespace(id='managed-networks'),
            deadline=time.monotonic() + max(0, deadline - self.now()),
            cancelled=(lambda: False) if cleanup else self.cancelled,
            now=time.monotonic, sleep=time.sleep)

    def _prepare_network(self, deadline):
        # Keep the Compose declaration unchanged: adding labels there changes its
        # network hash and may recreate an existing network on upgrade. Explicit
        # creation marks only NEW networks, with Compose's normal ownership labels
        # but no config hash. Compose can adopt those without a migration.
        name = self.scope.project + '_default'
        found = self._command((self.plan.command[0], 'network', 'ls', '--filter',
            'name=^' + name + '$', '--format', '{{.Name}}'), deadline).split()
        if found:
            if found != [name]:
                raise PrerequisiteError('Unexpected prerequisite network inventory; no network was changed.')
            try:
                record, = json.loads(self._command(
                    (self.plan.command[0], 'network', 'inspect', name), deadline))
                labels = record.get('Labels', record.get('labels'))
                if (record.get('Name', record.get('name')) != name
                        or not isinstance(labels, dict)
                        or labels.get('com.docker.compose.project') != self.scope.project
                        or labels.get('com.docker.compose.network') != 'default'):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError) as error:
                raise PrerequisiteError('Existing prerequisite network ownership could not be verified; no network was changed.') from error
            return
        self._command((self.plan.command[0], 'network', 'create',
            '--label', 'api-agent.network-cleanup-v1=' + self.scope.id,
            '--label', 'api-agent.managed=true',
            '--label', 'api-agent.scope=' + self.scope.id,
            '--label', 'com.docker.compose.project=' + self.scope.project,
            '--label', 'com.docker.compose.network=default', name), deadline)

    def _prune_networks(self, deadline, *, aged=False):
        if not self.plan.enabled:
            return
        # Stopped definitions still need their networks for restart, even when
        # the engine considers a network with no live endpoints pruneable.
        # Hold the creation lock over inspection and deletion, and remove only
        # pinned, explicitly opted-in networks with NO container references.
        executable, marker = self.plan.command[0], 'api-agent.network-cleanup-v1'
        cleanup = not aged
        with self._network_guard(deadline, cleanup=cleanup):
            references = None
            removable = []
            selected = marker if aged else marker + '=' + self.scope.id
            names = self._command((executable, 'network', 'ls', '--filter',
                'label=' + selected, '--format', '{{.Name}}'), deadline, cleanup=cleanup).split()
            names = sorted({name for name in names if re.fullmatch(r'sa-[0-9a-f]{16}_default', name)})
            for record in self._network_records(names, deadline, cleanup=cleanup):
                name = record.get('Name', record.get('name'))
                try:
                    labels = record.get('Labels', record.get('labels'))
                    scope = labels.get(marker) if isinstance(labels, dict) else None
                    if (not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{16}', scope)
                            or name != 'sa-' + scope + '_default'
                            or record.get('Name', record.get('name')) != name
                            or labels.get('api-agent.managed') != 'true'
                            or labels.get('api-agent.scope') != scope
                            or labels.get('com.docker.compose.project') != 'sa-' + scope
                            or labels.get('com.docker.compose.network') != 'default'
                            or (not aged and scope != self.scope.id)):
                        continue
                    identifier = record.get('Id', record.get('id'))
                    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                        raise ValueError()
                    if aged:
                        created = datetime.fromisoformat(record.get('Created', record.get('created')))
                        if created.tzinfo is None or (datetime.now(timezone.utc) - created).total_seconds() < 86400:
                            continue
                except (ValueError, TypeError, AttributeError) as error:
                    raise PrerequisiteError('Network cleanup inventory could not be verified; remaining networks were preserved.') from error
                if references is None:
                    references = self._container_network_references(deadline, cleanup=cleanup)
                if name in references or identifier in references:
                    continue
                removable.append(identifier)
            for offset in range(0, len(removable), 32):
                self._command((executable, 'network', 'rm', *removable[offset:offset + 32]),
                    deadline, cleanup=cleanup)

    def _network_records(self, names, deadline, *, cleanup):
        """Amortize supervised CLI startup across bounded inspection batches."""
        for offset in range(0, len(names), 32):
            batch = names[offset:offset + 32]
            try:
                records = json.loads(self._command(
                    (self.plan.command[0], 'network', 'inspect', *batch), deadline, cleanup=cleanup))
                if (not isinstance(records, list) or len(records) != len(batch)
                        or any(not isinstance(record, dict) for record in records)
                        or {record.get('Name', record.get('name')) for record in records} != set(batch)):
                    raise ValueError()
            except (ValueError, TypeError) as error:
                raise PrerequisiteError('Network cleanup inventory could not be verified; remaining networks were preserved.') from error
            yield from records

    def _container_network_references(self, deadline, *, cleanup):
        """Inspect saved definitions; network filters may only see live endpoints."""
        executable = self.plan.command[0]
        identifiers = self._command((executable, 'ps', '-a', '--no-trunc', '--format', '{{.ID}}'),
            deadline, cleanup=cleanup).split()
        references = set()
        try:
            if any(not _ID.fullmatch(identifier) for identifier in identifiers):
                raise ValueError()
            template = '{"networks":{{json .NetworkSettings.Networks}},"mode":{{json .HostConfig.NetworkMode}}}'
            for offset in range(0, len(identifiers), 32):
                batch = identifiers[offset:offset + 32]
                rows = self._command((executable, 'inspect', '--format', template, *batch),
                    deadline, cleanup=cleanup).splitlines()
                if len(rows) != len(batch):
                    raise ValueError()
                for row in rows:
                    entry = json.loads(row)
                    networks, mode = entry['networks'], entry['mode']
                    if ((networks is not None and not isinstance(networks, dict))
                            or (mode is not None and not isinstance(mode, str))):
                        raise ValueError()
                    if mode:
                        references.add(mode)
                    for name, network in (networks or {}).items():
                        references.add(name)
                        if network is not None:
                            identifier = network.get('NetworkID', '')
                            if not isinstance(identifier, str):
                                raise ValueError()
                            references.add(identifier)
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise PrerequisiteError('Container network references could not be verified; remaining networks were preserved.') from error
        return references

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
                if (not isinstance(labels, dict) or
                        (labels.get('com.docker.compose.service'), labels.get('api-agent.profile'))
                        not in self.inventory_pairs or not self.scope.owns(labels,
                            profiles=PROFILES | frozenset(self.plan.profiles) |
                                frozenset(profile for _, profile in self.inventory_pairs))):
                    raise PrerequisiteError('Compose project includes a container with foreign ownership.')
                from .prerequisite_diagnostics import initialization_changes
                service = labels['com.docker.compose.service']
                expected_environment = _interpolate(self.specs.get(service, {}).get('environment', {}), self.environment)
                changes = initialization_changes(service, config.get('Env'), expected_environment)
                result[identifier] = ManagedService(identifier, service,
                    config['Image'], record.get('Image'), labels.get(CONFIG_LABEL, ''), bool(state.get('Running')),
                    state.get('Health', {}).get('Status'), labels['api-agent.profile'], changes)
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise PrerequisiteError('Invalid prerequisite inspection response.') from error

    def _retire_disposed_duties(self, journal, inventory):
        """Retire only absent immutable IDs proved on the recorded engine."""
        for key, record in tuple(journal.items()):
            if (not record.get('cleanup_only', False)
                    or record.get('conditional_engine_id') != self.engine_id):
                continue
            remaining = sorted(set(record['conditional_cleanup_ids']).intersection(inventory))
            if remaining:
                journal[key] = {**record, 'ids': remaining, 'conditional_cleanup_ids': remaining, 'owned_ids': []}
            else:
                journal.pop(key)

    def status(self, *, _deadline=None):
        """Return bounded ownership-verified inventory without changing services."""
        deadline = self.now() + self.timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        _check(deadline, self.cancelled, self.now)
        inventory = self._inventory(deadline)
        current = self._compatible(inventory, deadline, allow_stopped=True)
        return tuple(current[name] for name in self.plan.services if name in current)

    def logs(self, identifier, *, tail=100, output_limit=16384, _deadline=None):
        """Read bounded scrubbed logs from a selected immutable owned object."""
        if (not isinstance(identifier, str) or not _ID.fullmatch(identifier)
                or type(tail) is not int or not 1 <= tail <= 1000
                or type(output_limit) is not int or not 256 <= output_limit <= 65536):
            raise PrerequisiteError('Invalid prerequisite log selection or bound.')
        deadline = self.now() + self.timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        inventory = self._inventory(deadline)
        current = self._compatible(inventory, deadline, allow_stopped=True)
        if identifier not in {item.id for item in current.values()}:
            raise PrerequisiteError('Prerequisite log selection is not owned by this plan.')
        from api_agent.testing.diagnostics import CredentialScrubber
        scrubber = CredentialScrubber({**self.environment, **{name + '.' + key: value
            for name, specification in self.specs.items()
            for key, value in specification.get('environment', {}).items()}})
        self._verify_engine()
        return self.runner((self.plan.command[0], 'logs', '--tail', str(tail), identifier),
            environment=self.environment, directory=self.plan.directory, deadline=deadline,
            cancelled=self.cancelled, now=self.now, combine_output=True,
            output_limit=output_limit, output_scrubber=scrubber)

    def _probe(self, profile, timeout, environment):
        self._verify_engine()
        return self.probe(profile, timeout, environment)

    def _conditional_ready(self, current, deadline, *, names=None, checks=None):
        for name, specification in self.conditional.items():
            if names is not None and name not in names:
                continue
            if name not in current:
                return False
            if 'runtime' in self.specs[name]:
                self.verify_conditional_storage(name, current[name].id, deadline)
            for readiness in ((specification.readiness, specification.seed_completion)
                              if checks is None else checks):
                if readiness is None:
                    continue
                _check(deadline, self.cancelled, self.now)
                self._verify_engine()
                if readiness.kind == 'health':
                    if current[name].health != 'healthy':
                        return False
                elif not _http_identity(readiness, min(1, readiness.timeout,
                        max(.001, deadline - self.now())), cancelled=self.cancelled):
                    return False
        return True

    def _wait_conditional(self, name, deadline, verified):
        observed = dict(verified)
        specification = self.conditional[name]
        for readiness in (specification.readiness, specification.seed_completion):
            if readiness is None:
                continue
            check_deadline = min(deadline, self.now() + readiness.timeout)
            while True:
                _check(check_deadline, self.cancelled, self.now)
                inventory = self._inventory(check_deadline)
                current = self._compatible(inventory, check_deadline, allow_stopped=True)
                if any(key not in current or current[key].id != item.id
                        or not current[key].running for key, item in observed.items()):
                    raise PrerequisiteError('Existing prerequisite changed during acquisition.')
                if name in current and not current[name].running:
                    # Managed services declare no restart policy, so a stopped
                    # container cannot still become ready; its logs explain why.
                    raise PrerequisiteError('Prerequisite service stopped before it became ready; inspect its logs.')
                if (name in current
                        and self._conditional_ready(current, check_deadline,
                            names={name}, checks=(readiness,))):
                    observed[name] = current[name]
                    break
                self.sleep(.1)
        self.verify_conditional_storage(name, observed[name].id, deadline)
        return observed[name]

    def verify_conditional_storage(self, name, identifier, deadline, *, cleanup=False):
        """Verify every retained bind against captured bytes and scoped volumes."""
        specification = self.conditional[name]
        try:
            record, = json.loads(self._command((self.plan.command[0], 'inspect', identifier),
                deadline, cleanup=cleanup))
            if record['Id'] != identifier:
                raise ValueError()
            from .tool_policy import verify_isolation
            try:
                verify_isolation(self.specs[name], record, network=self.scope.project + '_default')
            except ValueError as error:
                raise PrerequisiteError('Prerequisite isolation could not be verified.') from error
            mounts = record['Mounts']
            expected = self.specs[name].get('volumes', [])
            if len(mounts) != len(expected):
                raise ValueError()
            missing_capture = None
            for index, volume in enumerate(expected):
                template = self.document['services'][name]['volumes'][index]
                source, target, *options = volume.rsplit(':', 2)
                actual, = [item for item in mounts if item['Destination'] == target]
                asset_key = next((key for key in specification.asset_digests
                    if template.startswith('${' + key + ':?')), None)
                if asset_key is not None:
                    path = self._host_bind_source(actual['Source'], deadline, cleanup=cleanup)
                    if (actual['Type'] != 'bind' or actual.get('RW') is not False
                            or not path.is_relative_to(self.directory)):
                        raise ValueError()
                    try:
                        inside = len(path.parent.relative_to(self.directory).parts) + 1
                        for parent in (path.parent, *path.parent.parents)[:inside]:
                            info = parent.lstat()
                            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                                    or info.st_mode & 0o022):
                                raise ValueError()
                        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(descriptor, 'rb') as stream:
                            info = os.fstat(stream.fileno())
                            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                    or info.st_mode & 0o022 or info.st_size > 1024 * 1024
                                    or sha256(stream.read(1024 * 1024 + 1)).hexdigest()
                                    != specification.asset_digests[asset_key]):
                                raise ValueError()
                    except FileNotFoundError as error:
                        missing_capture = error
                else:
                    volume_name = self.scope.project + '_' + source
                    if actual['Type'] != 'volume' or actual.get('Name') != volume_name:
                        raise ValueError()
                    owned, = json.loads(self._command((self.plan.command[0], 'volume', 'inspect',
                        volume_name), deadline, cleanup=cleanup))
                    labels = self.scope.labels(specification.profile, profiles=self.plan.profiles)
                    if (owned['Name'] != volume_name or any(owned['Labels'].get(key) != value
                            for key, value in labels.items())):
                        raise ValueError()
            if missing_capture is not None:
                from .prerequisite_diagnostics import missing_captured_assets_message
                raise MissingCapturedAssetsError(missing_captured_assets_message((identifier,),
                    executable=self.plan.command[0], project=self.scope.project)) from missing_capture
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            raise PrerequisiteError('Prerequisite storage or captured assets could not be verified.') from error

    def _host_bind_source(self, source, deadline, *, cleanup=False):
        """The host path an inspected bind names.

        Docker Desktop reports binds as its VM mounts them, under /host_mnt. Only
        that engine's prefix is removed; on any other engine the path is literal.
        """
        path = Path(source)
        if not path.is_relative_to('/host_mnt'):
            return path
        reported = json.loads(self._command((self.plan.command[0], 'info', '--format', '{{json .OperatingSystem}}'),
                                            deadline, cleanup=cleanup))
        return Path('/', *path.parts[2:]) if reported == 'Docker Desktop' else path

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
            source = self._host_bind_source(hook['Source'], deadline, cleanup=cleanup)
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

    def verify_jaeger_storage(self, identifier, deadline, *, cleanup=False, recover=None):
        """Adopt only the owned immutable config bind and scoped Badger volume."""
        restored = False
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
            source = self._host_bind_source(bind['Source'], deadline, cleanup=cleanup)
            parent = source.parent
            if (bind['Type'] != 'bind' or bind.get('RW') is not False
                    or source.name != 'jaeger-config.json' or not parent.is_relative_to(self.directory)):
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
            if recover is not None:
                labels = record['Config']['Labels']
                if (record['State']['Running'] is not False or record['Config']['Image'] != recover.image
                        or record.get('Image') != recover.image_id
                        or labels.get(CONFIG_LABEL) != recover.fingerprint
                        or labels.get('api-agent.profile') != recover.profile
                        or labels.get('com.docker.compose.service') != 'jaeger'
                        or not self.scope.owns(labels, profiles=PROFILES)):
                    raise ValueError()
                try:
                    parent.lstat()
                except FileNotFoundError:
                    self._restore_jaeger_snapshot(parent, deadline)
                    restored = True
            inside_count = len(parent.relative_to(self.directory).parts) + 1
            for directory in (parent, *parent.parents)[:inside_count]:
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022):
                    raise ValueError()
            # Compatibility is service-scoped: the fingerprint verifies Jaeger's
            # declaration; only its mounted config belongs to this retained bind.
            # Other snapshot assets may predate unrelated service additions.
            expected = self.plan.assets['jaeger-config.json']
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022 or info.st_size > len(expected)
                        or stream.read(len(expected) + 1) != expected):
                    raise ValueError()
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            raise PrerequisiteError('Jaeger storage ownership or captured assets could not be verified.') from error
        return restored

    def _restore_jaeger_snapshot(self, parent, deadline):
        """Restore only an entirely absent snapshot at its original owned bind."""
        if (parent.parent != self.directory or not re.fullmatch(
                re.escape(self.scope.id) + r'-assets-[a-z0-9_]{8}', parent.name)):
            raise ValueError()
        _check(deadline, self.cancelled, self.now)
        root = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(root)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError()
            os.mkdir(parent.name, 0o700, dir_fd=root)
            created = os.stat(parent.name, dir_fd=root, follow_symlinks=False)
            try:
                child = os.open(parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            except OSError:
                try:
                    current = os.stat(parent.name, dir_fd=root, follow_symlinks=False)
                    root_path = self.directory.lstat()
                    if ((current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
                            and (root_path.st_dev, root_path.st_ino) == (info.st_dev, info.st_ino)):
                        os.rmdir(parent.name, dir_fd=root)
                except OSError:
                    pass
                raise
            try:
                opened = os.fstat(child)
                if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()
                        or opened.st_mode & 0o022 or (opened.st_dev, opened.st_ino) != (created.st_dev, created.st_ino)):
                    raise ValueError()
                file_info = None
                try:
                    descriptor = os.open('jaeger-config.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o444, dir_fd=child)
                    with os.fdopen(descriptor, 'wb') as stream:
                        file_info = os.fstat(stream.fileno())
                        stream.write(self.plan.assets['jaeger-config.json'])
                        os.fchmod(stream.fileno(), 0o444)
                    current = os.stat(parent.name, dir_fd=root, follow_symlinks=False)
                    root_path = self.directory.lstat()
                    if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                            or (root_path.st_dev, root_path.st_ino) != (info.st_dev, info.st_ino)):
                        raise ValueError()
                except BaseException:
                    # Remove only objects this attempt created and can still
                    # identify; any replacement or unexpected entry stays intact.
                    try:
                        current = os.stat(parent.name, dir_fd=root, follow_symlinks=False)
                        root_path = self.directory.lstat()
                        if ((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
                                and (root_path.st_dev, root_path.st_ino) == (info.st_dev, info.st_ino)):
                            if file_info is not None:
                                current_file = os.stat('jaeger-config.json', dir_fd=child, follow_symlinks=False)
                                if (current_file.st_dev, current_file.st_ino) == (file_info.st_dev, file_info.st_ino):
                                    descriptor = os.open('jaeger-config.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                                         dir_fd=child)
                                    with os.fdopen(descriptor, 'rb') as stream:
                                        read_info = os.fstat(stream.fileno())
                                        expected = self.plan.assets['jaeger-config.json']
                                        content = stream.read(len(expected) + 1)
                                        current_file = os.stat('jaeger-config.json', dir_fd=child, follow_symlinks=False)
                                        if ((read_info.st_dev, read_info.st_ino) == (file_info.st_dev, file_info.st_ino)
                                                and (current_file.st_dev, current_file.st_ino) == (file_info.st_dev, file_info.st_ino)
                                                and expected.startswith(content)):
                                            os.unlink('jaeger-config.json', dir_fd=child)
                            os.rmdir(parent.name, dir_fd=root)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(child)
        finally:
            os.close(root)

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
            elif re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
                # Engines can retain the source tag as Config.Image for an ID pin.
                # The running image ID, not that display name, proves identity.
                image_matches = item.image_id in {expected, expected.removeprefix('sha256:')}
            else:
                image_matches = item.image == expected
            if (item.service in desired or not image_matches
                    or item.fingerprint != self.fingerprints[item.service] or (not item.running and not allow_stopped)
                    or item.profile != self.specs[item.service]['profiles'][0]):
                from .prerequisite_diagnostics import incompatible_stack_message
                raise PrerequisiteError(incompatible_stack_message(item,
                    configuration_changed=item.fingerprint != self.fingerprints[item.service],
                    executable=self.plan.command[0], project=self.scope.project))
            desired[item.service] = item
        return desired

    def _stop_new(self, identifiers, journal, deadline):
        protected = _leased_ids(journal)
        eligible = set(identifiers) - protected
        if not eligible:
            return ()
        current = self._inventory(deadline, cleanup=True)
        owned = tuple(sorted(item for item in eligible if item in current and current[item].running))
        # Stop consumers before their stores, independently of engine scheduling
        # or immutable container-ID order. Prepare all tiers before mutating any
        # service so an invalid dependency cycle retains the complete stack.
        pending, tiers = set(owned), []
        while pending:
            dependencies = {name for identifier in pending
                            for name in self.specs[current[identifier].service].get('depends_on', {})}
            tier = tuple(sorted(identifier for identifier in pending
                                if current[identifier].service not in dependencies))
            if not tier:
                raise PrerequisiteError('Prerequisite cleanup dependency cycle; resources remain owned.')
            tiers.append(tier)
            pending.difference_update(tier)
        for tier in tiers:
            self._command((self.plan.command[0], 'stop', '--time', '5', *tier), deadline, cleanup=True)
        return owned

    def _rollback_acquisition(self, journal, deadline):
        newly, snapshot, invocation = self._rollback
        if newly is None:
            raise PrerequisiteError('Initial rollback ownership could not be verified; review the failed acquisition before cleanup.')
        journal = dict(journal)
        journal.pop(invocation, None)
        stopped = self._stop_new(newly, journal, deadline)
        self._prune_networks(deadline)
        _write_journal(self.directory, self.scope, journal)
        if snapshot is not None and not newly:
            shutil.rmtree(snapshot)
            self.snapshot_directory = None
        self._rollback = None
        return stopped

    def acquire(self, *, expected_ids=None, reporting_directory=None, _prelocked=False, _deadline=None):
        if self._rollback is not None:
            raise PrerequisiteError('Prerequisite rollback remains pending; release before acquiring again.')
        if self.lease is not None:
            return self.lease
        deadline = self.now() + self.timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        runtimes = {spec['runtime'] for spec in self.specs.values() if 'runtime' in spec}
        if runtimes:
            try:
                if self.engine_target is None:
                    available = json.loads(self._command((self.plan.command[0], 'info', '--format', '{{json .Runtimes}}'), deadline))
                else:
                    from .engine_targets import runtime_inventory
                    remaining = deadline - self.now()
                    if remaining <= 0:
                        raise PrerequisiteError('Prerequisite runtime discovery deadline expired.')
                    available = runtime_inventory(self.engine_target, cancelled=self.cancelled,
                                                  timeout_seconds=min(10, remaining))
            except ValueError as error:
                raise PrerequisiteError('Required isolated runtime inventory is invalid.') from error
            if not isinstance(available, dict) or not runtimes <= set(available):
                raise PrerequisiteError('Required isolated runtime is unavailable on the selected engine.')
        with (nullcontext() if _prelocked else _locked(self.directory, self.scope, deadline=deadline,
                     cancelled=self.cancelled, now=self.now, sleep=self.sleep)):
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
            self._retire_disposed_duties(journal, initial)
            if expected_ids is not None and (set(initial) != set(expected_ids)
                    or (reporting_directory is None and any(not item.running for item in initial.values()))):
                raise PrerequisiteError('Registered reporting container changed or stopped; no restart was attempted.')
            compatible = self._compatible(initial, deadline, allow_stopped=True)
            restarted = {item.id for item in compatible.values() if not item.running}
            protected = _leased_ids(journal)
            if restarted & protected:
                raise PrerequisiteError('Stopped prerequisite has an active invocation lease.')
            missing_captures = []
            for name, item in compatible.items():
                if name in self.conditional:
                    try:
                        self.verify_conditional_storage(name, item.id, deadline)
                    except MissingCapturedAssetsError as error:
                        missing_captures.append((item.id, error))
            recovered_id = None
            if 'jaeger' in compatible:
                jaeger = compatible['jaeger']
                if self.verify_jaeger_storage(jaeger.id, deadline, recover=None if jaeger.running else jaeger):
                    recovered_id = jaeger.id
            if reporting_directory is not None:
                if set(compatible) != {'localstack'}:
                    raise PrerequisiteError('Registered reporting service changed; no restart was attempted.')
                for identifier in expected_ids:
                    self.verify_reporting_storage(identifier, reporting_directory, deadline)
            if missing_captures:
                from .prerequisite_diagnostics import missing_captured_assets_message
                identifiers = tuple(sorted(identifier for identifier, _ in missing_captures))
                raise MissingCapturedAssetsError(missing_captured_assets_message(identifiers,
                    executable=self.plan.command[0], project=self.scope.project)) from missing_captures[0][1].__cause__
            self._prune_networks(deadline, aged=True)
            existing = {name: item for name, item in compatible.items() if item.running}
            newly = ()
            snapshot = None
            missing = tuple(name for name in self.plan.services if name not in existing)
            verified = dict(existing)
            try:
                if recovered_id is not None:
                    self._command((self.plan.command[0], 'start', recovered_id), deadline)
                if restarted and reporting_directory is not None:
                    # Restart only the registered immutable objects. Compose up
                    # would select a fresh snapshot rather than the retained bind.
                    self._command((self.plan.command[0], 'start', *sorted(restarted)), deadline)
                elif any(name != 'jaeger' or recovered_id is None for name in missing):
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
                    self.environment = MappingProxyType({**self.environment, **environment, **self.engine_environment})
                    fixed_missing = tuple(name for name in missing if name not in self.conditional
                                          and not (name == 'jaeger' and recovered_id is not None))
                    if fixed_missing:
                        self._command((*command, '-f', str(override), 'up', '-d',
                            '--no-recreate', *fixed_missing), deadline)
                for name, specification in self.conditional.items():
                    if name in missing:
                        self._command((*command, '-f', str(override), 'up', '-d', '--no-deps',
                            '--no-recreate', name), deadline)
                    verified[name] = self._wait_conditional(name, deadline, verified)
                while True:
                    _check(deadline, self.cancelled, self.now)
                    inventory = self._inventory(deadline)
                    newly = tuple(item for item in inventory if (item not in initial or item in restarted)
                        and inventory[item].service in missing)
                    current = self._compatible(inventory, deadline)
                    if recovered_id is not None and ('jaeger' not in current or current['jaeger'].id != recovered_id):
                        raise PrerequisiteError('Recovered prerequisite changed during acquisition.')
                    if any(name not in current or current[name].id != item.id
                           for name, item in verified.items()):
                        raise PrerequisiteError('Existing prerequisite changed during acquisition.')
                    if not self._conditional_ready(current, deadline):
                        raise PrerequisiteError('Verified prerequisite readiness changed during acquisition.')
                    if (len(current) == len(self.specs)
                            and all(item.health in {None, 'healthy'} for item in current.values())
                            and all(self._probe(profile, min(1, max(.001, deadline - self.now())),
                                    self.environment) for profile in self.plan.profiles
                                    if profile not in {item.profile for item in self.conditional.values()})):
                        _check(deadline, self.cancelled, self.now)
                        for name, item in current.items():
                            if name in self.conditional:
                                self.verify_conditional_storage(name, item.id, deadline)
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
                    'owned_ids': list(newly), 'reused_ids': list(reused), 'fingerprints': self.fingerprints,
                    'conditional_engine_id': self.engine_id,
                    'conditional_cleanup_ids': [identifier for identifier in newly
                        if inventory[identifier].service in self.conditional]}
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
                        and inventory[item].service in missing
                        and (inventory[item].service != 'jaeger' or recovered_id is None or item == recovered_id)
                        and (inventory[item].service not in verified
                            or item == verified[inventory[item].service].id))
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

    def release(self, *, retain=False, reporting_ids=None, reporting_directory=None,
                _prelocked=False, _deadline=None):
        if self.lease is None and self._rollback is None:
            return ()
        deadline = self.now() + self.cleanup_timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        with (nullcontext() if _prelocked else _locked(self.directory, self.scope, deadline=deadline,
                     cancelled=lambda: False, now=self.now, sleep=self.sleep)):
            if self.lease is None and self._rollback is None:
                return ()
            journal = _read_journal(self.directory, self.scope, self.identity)
            if self._rollback is not None:
                return self._rollback_acquisition(journal, deadline)
            if self.conditional and any(record.get('cleanup_only', False) for record in journal.values()):
                self._retire_disposed_duties(journal, self._inventory(deadline, cleanup=True))
            record = journal.pop(self.lease.invocation_id, {})
            cleanup_ids = set(record.get('conditional_cleanup_ids', []))
            if record.get('conditional_engine_id') == self.engine_id:
                cleanup_ids.update(record.get('owned_ids', []))
            if not retain:
                for key in sorted(journal):
                    successor = dict(journal[key])
                    if (successor.get('cleanup_only', False)
                            or successor.get('conditional_engine_id') != self.engine_id):
                        continue
                    transferred = cleanup_ids.intersection(successor['ids'])
                    if transferred:
                        successor['conditional_cleanup_ids'] = sorted(
                            set(successor.get('conditional_cleanup_ids', [])) | transferred)
                        journal[key] = successor
                        cleanup_ids -= transferred
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
                protected = _leased_ids(journal)
                eligible = tuple(sorted(item for item in reporting_ids
                    if item not in protected and current[item].running))
                if eligible:
                    self._command((self.plan.command[0], 'stop', '--time', '5', *eligible), deadline, cleanup=True)
                stopped = eligible
            else:
                stopped = () if retain else self._stop_new(tuple(sorted(set(self.lease.owned_ids) | cleanup_ids)), journal, deadline)
            if not retain:
                self._prune_networks(deadline)
            _write_journal(self.directory, self.scope, journal)
            # Both running and stopped container definitions keep their original
            # bind source. Removal/asset garbage collection requires an explicit
            # inventory of those definitions, beyond this stop-only lifecycle.
            # Forget local cleanup ownership after the lease is relinquished.
            self.snapshot_directory = None
            self.lease = None
            return stopped
