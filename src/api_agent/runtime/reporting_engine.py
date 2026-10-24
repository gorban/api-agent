"""Read-only identity handshake for a selected local container-engine transport."""
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from .compose import controlled_engine_environment
from .prerequisites import PrerequisiteError, PrerequisiteCancelled, run_command
from api_agent.providers.progress import report_timeout_sync


class DeadlineSocket(socket.socket):
    """SocketIO/makefile reads retain an absolute budget despite trickle traffic."""
    def _remaining(self):
        if self.cancelled():
            raise PrerequisiteCancelled('Reporting engine verification cancelled.')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Reporting engine deadline expired')
        return remaining

    def recv_into(self, buffer, nbytes=0, flags=0):
        while True:
            self.settimeout(min(.1, self._remaining()))
            try:
                return super().recv_into(buffer, nbytes, flags)
            except TimeoutError:
                self._remaining()

    def sendall(self, data, flags=0):
        self.settimeout(self._remaining())
        return super().sendall(data, flags)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=5)
        self.path = path
        self.deadline = time.monotonic() + self.timeout
        self.cancelled = lambda: False

    def connect(self):
        self.sock = DeadlineSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.deadline, self.sock.cancelled = self.deadline, self.cancelled
        try:
            self.sock.settimeout(self.sock._remaining())
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


def verified_engine_binding(environment=None, *, executable='docker', runner=run_command,
                            connection=UnixConnection, cancelled=lambda: False):
    """Return identity and canonical Unix transport after one CLI/direct handshake."""
    environment = controlled_engine_environment(environment)
    selected = {environment[key] for key in ('DOCKER_HOST', 'CONTAINER_HOST') if environment.get(key)}
    if selected:
        if len(selected) != 1 or not next(iter(selected)).startswith('unix:///'):
            raise PrerequisiteError('Shared reporting requires an unambiguous local Unix engine transport.')
        paths = [Path(next(iter(selected))[7:])]
    else:
        runtime = environment.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        paths = [path for path in (Path(runtime) / 'podman/podman.sock', Path('/var/run/docker.sock'))
                 if path.exists() or path.is_symlink()]
    try:
        resolved = {}
        aliases = []
        for candidate in paths:
            if not candidate.is_absolute():
                raise ValueError()
            for item in (candidate, *candidate.parents):
                evidence = item.lstat()
                if evidence.st_uid not in {0, os.getuid()}:
                    raise ValueError()
                if (stat.S_ISDIR(evidence.st_mode) and evidence.st_mode & 0o022
                        and not (evidence.st_uid == 0 and evidence.st_mode & stat.S_ISVTX)):
                    raise ValueError()
            physical = candidate.resolve(strict=True)
            info = physical.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in {0, os.getuid()}:
                raise ValueError()
            for parent in physical.parents:
                evidence = parent.stat()
                if (evidence.st_uid not in {0, os.getuid()} or (evidence.st_mode & 0o022
                        and not (evidence.st_uid == 0 and evidence.st_mode & stat.S_ISVTX))):
                    raise ValueError()
            resolved[(info.st_dev, info.st_ino, info.st_uid)] = physical
            aliases.append((candidate, physical))
        if len(resolved) != 1:
            raise ValueError()
        generation, physical = next(iter(resolved.items()))
        def get(route):
            client = connection(str(physical))
            client.timeout = max(.001, budget.remaining)
            client.deadline = time.monotonic() + budget.remaining
            client.cancelled = cancelled
            response = None
            try:
                client.request('GET', route)
                response = client.getresponse()
                body = response.read(1024 * 1024 + 1)
                if response.status != 200 or len(body) > 1024 * 1024:
                    raise ValueError()
                return json.loads(body)
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    client.close()
        with report_timeout_sync('Reporting engine identity', 5) as budget:
            direct = get('/info')
            containers = get('/containers/json?all=1')
            def command(arguments):
                return runner((executable, *arguments), environment=environment, directory=physical.parent,
                              deadline=time.monotonic() + budget.remaining, cancelled=cancelled,
                              now=time.monotonic)
            cli = json.loads(command(('info', '--format', '{{json .}}')))
            identifiers = command(('ps', '-a', '--no-trunc', '--format', '{{.ID}}')).split()
        engine_id = direct['ID']
        if not isinstance(engine_id, str) or not re.fullmatch('[A-Za-z0-9:-]{1,128}', engine_id):
            raise ValueError()
        if cli.get('ID'):
            matches = cli['ID'] == engine_id
            stable_identity = engine_id
        else:
            hostname, store = cli['host']['hostname'], cli['store']['graphRoot']
            if (not isinstance(hostname, str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,253}', hostname)
                    or not isinstance(store, str) or not store.startswith('/')
                    or len(store) > 4096 or not store.isprintable()):
                raise ValueError()
            matches = (bool(identifiers) and hostname == direct['Name']
                       and store == direct['DockerRootDir'])
            # Compatibility response IDs need not be persistent. Namespace the
            # corroborated Unix transport separately from a matched CLI ID.
            stable_identity = ['corroborated-unix-v1', hostname, store]
        direct_ids = [item['Id'] for item in containers]
        if (not matches or any(not re.fullmatch('[0-9a-f]{64}', value) for value in identifiers + direct_ids)
                or len(set(identifiers)) != len(identifiers) or len(set(direct_ids)) != len(direct_ids)
                or sorted(identifiers) != sorted(direct_ids)
                or any(candidate.resolve(strict=True) != target for candidate, target in aliases)
                or (physical.stat().st_dev, physical.stat().st_ino, physical.stat().st_uid) != generation):
            raise ValueError()
        value = json.dumps([stable_identity, str(physical), *generation])
        return sha256(value.encode()).hexdigest(), 'unix://' + str(physical)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException) as error:
        raise PrerequisiteError('Cannot verify shared reporting engine identity; no service was adopted.') from error


def engine_identity(environment=None, *, executable='docker', runner=run_command,
                    connection=UnixConnection, cancelled=lambda: False):
    """Preserve the reporting identity contract over the shared verification."""
    identity, _ = verified_engine_binding(environment, executable=executable, runner=runner,
                                           connection=connection, cancelled=cancelled)
    return identity
