"""Worker-only selected-profile persistence with byte-preserving file updates."""
import configparser
import contextlib
import hashlib
import json
import math
import time
import os
from pathlib import Path
import re
import stat
import threading

LIMIT = 1048576
KEYS = ('aws_access_key_id', 'aws_secret_access_key', 'aws_session_token')
MARKER = 'api_agent_login_snapshot'


def capture_paths():
    """Freeze SDK-expanded locations before changing HOME or working directory."""
    home = os.path.expanduser('~')
    values = dict(home=home,
        config=os.environ.get('AWS_CONFIG_FILE', os.path.join(home, '.aws/config')),
        credentials=os.environ.get('AWS_SHARED_CREDENTIALS_FILE', os.path.join(home, '.aws/credentials')),
        login=os.environ.get('AWS_LOGIN_CACHE_DIRECTORY', os.path.join(home, '.aws/login/cache')))
    return {key: os.path.abspath(os.path.expanduser(os.path.expandvars(value))) for key, value in values.items()}


@contextlib.contextmanager
def directory_fd(path, *, create=False):
    """Anchor every component without following links, including rename races."""
    parts = Path(os.path.abspath(path)).parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_at(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return b''
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Unsafe AWS persistence file')
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('AWS persistence file exceeds limit')
    return data


def read(path):
    """Read a bounded regular file using link-rejecting directory handles."""
    path = Path(path)
    try:
        with directory_fd(path.parent) as directory:
            return read_at(directory, path.name)
    except FileNotFoundError:
        return b''


def parsed(data):
    result = configparser.RawConfigParser(strict=True)
    result.read_string(data.decode('utf-8'))
    return result


def section(data, name):
    """Extract an exact INI section while preserving all original bytes."""
    matches = list(re.finditer(rb'^\[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)', data, re.M))
    for index, match in enumerate(matches):
        if match[1].decode('utf-8') == name:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(data)
            return match.start(), end, data[match.start():end]
    return len(data), len(data), b''


def replace_section(data, name, replacement):
    start, end, _ = section(data, name)
    prefix = data[:start]
    if prefix and not prefix.endswith(b'\n'):
        prefix += b'\n'
    return prefix + replacement + data[end:]


def merge_fields(data, name, values):
    """Replace specified INI options only; preserve comments and other settings."""
    block = section(data, name)[2]
    if not block:
        block = ('[' + name + ']\n').encode()
    lines = block.splitlines(keepends=True)
    retained = []
    for line in lines:
        option = re.match(rb'^[ \t]*([^#;\s][^=:]*?)[ \t]*[=:]', line)
        if option is None or option[1].decode().strip().lower() not in values:
            retained.append(line)
    block = b''.join(retained)
    if not block.endswith(b'\n'):
        block += b'\n'
    block += ''.join(key + ' = ' + value + '\n' for key, value in values.items() if value is not None).encode()
    return replace_section(data, name, block)



def atomic(path, data, expected):
    """Compare before replace; staging is private and removed on every failure."""
    path = Path(path)
    with directory_fd(path.parent, create=True) as directory:
        if read(path) != expected:
            raise ValueError('AWS persistence changed concurrently')
        temporary = '.api-agent-' + os.urandom(16).hex()
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if read(path) != expected or read_at(directory, path.name) != expected:
                raise ValueError('AWS persistence changed concurrently')
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ProfileStore:
    """All methods execute inside an owned, deadline-bounded login worker."""
    def __init__(self, paths, profile):
        self.config = Path(paths['config'])
        self.credentials = Path(paths['credentials'])
        self.home = Path(paths['home'])
        self.login = Path(paths['login'])
        if any(not path.is_absolute() for path in (self.config, self.credentials, self.home, self.login)):
            raise ValueError('AWS persistence paths must be absolute')
        self.profile = profile
        self.name = 'default' if profile == 'default' else 'profile ' + profile
        self._baseline = None
        self._thread_lock = threading.RLock()
        self._lock_depth = 0

    @contextlib.contextmanager
    def locked(self):
        """Serialize complete readiness/publication transactions, including nesting."""
        import fcntl
        # The depth belongs to the thread holding this mutex. A different thread
        # using this same store must still wait, just like a separate worker.
        with self._thread_lock:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            with directory_fd(self.credentials.parent, create=True) as directory:
                fd = os.open('.api-agent-login.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('Unsafe AWS persistence lock')
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
            finally:
                os.close(fd)

    def cache_paths(self, data):
        config = parsed(data)
        route = dict(config[self.name]) if config.has_section(self.name) else {}
        if route.get('login_session'):
            key = hashlib.sha256(route['login_session'].encode()).hexdigest()
            return [(self.login / (key + '.json'), Path('login/cache') / (key + '.json'))]
        session = route.get('sso_session')
        sso = 'sso-session ' + session if session else None
        start = route.get('sso_start_url')
        if sso and config.has_section(sso):
            start = config[sso].get('sso_start_url')
        if session or start:
            key = hashlib.sha1((session or start).encode()).hexdigest()
            return [(self.home / '.aws/sso/cache' / (key + '.json'), Path('sso/cache') / (key + '.json'))]
        return []

    def selected_config(self, data):
        config = parsed(data)
        own = section(data, self.name)[2]
        if config.has_section(self.name) and config[self.name].get('sso_session'):
            related = section(data, 'sso-session ' + config[self.name]['sso_session'])[2]
            if related and not own.endswith(b'\n'):
                own += b'\n'
            own += related
        return own

    def remember(self):
        if self._baseline is None:
            self._baseline = (section(read(self.config), self.name)[2],
                              section(read(self.credentials), self.profile)[2])

    def snapshot(self):
        data = read(self.credentials)
        config = parsed(data)
        if not config.has_section(self.profile) or MARKER not in config[self.profile]:
            return None
        values = config[self.profile]
        marker = json.loads(values[MARKER])
        if set(marker) != {'fingerprint', 'identity', 'expires_at'}:
            raise ValueError('Invalid AWS snapshot metadata')
        # A deliberate user edit selects their credentials, rather than our cache.
        if fingerprint([values.get(key) for key in KEYS]) != marker['fingerprint']:
            return None
        self.remember()
        return marker

    def unmasked_credentials(self):
        data = read(self.credentials)
        if self.snapshot() is None:
            return data
        # This file is only an isolated worker input. Other sections stay intact.
        return replace_section(data, self.profile, b'')

    def seed(self, root, *, configured=False):
        """Copy only selected routing and exact SDK cache keys into owned HOME."""
        self.remember()
        root = Path(root) / '.aws'
        existing = read(self.config)
        own = self.selected_config(existing)
        target = root / 'config'
        if own and not configured:
            atomic(target, own, read(target))
        for source, relative in self.cache_paths(read(target) if configured else existing):
            value = read(source)
            if value:
                atomic(root / relative, value, read(root / relative))

    def publish(self, root, material, expiry, identity):
        """Caller must verify this exact frozen tuple with STS first."""
        root = Path(root) / '.aws'
        incoming = read(root / 'config')
        route = parsed(incoming)
        if not route.has_section(self.name) or not self.cache_paths(incoming):
            raise ValueError('Missing refreshable AWS profile')
        values = [material[key] for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')]
        if (any(type(value) is not str or not value.strip() or any(ord(char) < 32 for char in value) for value in values)
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()):
            raise ValueError('Invalid AWS snapshot material')
        marker = dict(fingerprint=fingerprint(values), identity=identity, expires_at=expiry)
        snapshot = dict(zip(KEYS, values))
        snapshot[MARKER] = json.dumps(marker, separators=(',', ':'))
        with self.locked():
            old_config, old_credentials = read(self.config), read(self.credentials)
            if self._baseline is not None and self._baseline != (section(old_config, self.name)[2], section(old_credentials, self.profile)[2]):
                raise ValueError('Selected AWS profile changed concurrently')
            # Avoid overwriting a static profile intentionally supplied by its owner.
            current = parsed(old_credentials)
            if current.has_section(self.profile) and any(key in current[self.profile] for key in KEYS) and self.snapshot() is None:
                raise ValueError('Selected AWS credentials changed')
            updates = {key: route[self.name].get(key) for key in
                       ('region', 'login_session', 'sso_session', 'sso_start_url', 'sso_region', 'sso_account_id', 'sso_role_name')}
            updated = merge_fields(old_config, self.name, updates)
            sso = route[self.name].get('sso_session')
            if sso:
                name = 'sso-session ' + sso
                old = section(old_config, name)[2]
                new = section(incoming, name)[2]
                if old and old != new:
                    raise ValueError('Shared AWS SSO routing changed')
                updated = replace_section(updated, name, new)
            writes = []
            for target, relative in self.cache_paths(incoming):
                payload = read(root / relative)
                if not payload:
                    raise ValueError('Missing AWS refresh cache')
                writes.append((target, payload, read(target)))
            writes.extend(((self.config, updated, old_config),
                           (self.credentials, merge_fields(old_credentials, self.profile, snapshot), old_credentials)))
            # Check the complete read set before the first mutation.
            if any(read(path) != before for path, _, before in writes):
                raise ValueError('AWS persistence changed concurrently')
            applied = []
            existed = {path: path.exists() for path, _, _ in writes}
            try:
                for path, after, before in writes:
                    atomic(path, after, before)
                    applied.append((path, before, after))
            except Exception:
                for path, before, after in reversed(applied):
                    if existed[path]:
                        atomic(path, before, after)
                    elif read(path) == after:
                        path.unlink()
                    else:
                        raise ValueError('AWS persistence changed during rollback') from None
                raise
            self._baseline = (section(updated, self.name)[2], section(merge_fields(old_credentials, self.profile, snapshot), self.profile)[2])
