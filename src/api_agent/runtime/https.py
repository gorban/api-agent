"""Explicit preparation of local development TLS material.

Trust installed by mkcert applies to the environment running mkcert. A browser on
another host needs the public CA installed separately in that host's trust store.
"""

from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
import fcntl
import ipaddress
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time


class HTTPSPreparationError(RuntimeError):
    """Local HTTPS could not be prepared; message contains no command output."""


@dataclass(frozen=True)
class TLSMaterial:
    cert_file: Path
    key_file: Path
    ca_file: Path


@dataclass(frozen=True)
class HTTPSPreparation:
    material: TLSMaterial
    diagnostics: tuple[str, ...]


def _default_runner(argv, *, timeout, interactive):
    visible = interactive and argv[:2] == ['mkcert', '-install']
    return subprocess.run(
        argv, capture_output=not visible, text=True, check=False, timeout=timeout,
        stdin=None if interactive else subprocess.DEVNULL,
    )


def _invoke(runner, argv, *, timeout, interactive, label):
    try:
        result = runner(argv, timeout=timeout, interactive=interactive)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPSPreparationError(f'{label} could not start; install mkcert and OpenSSL and check file permissions') from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPSPreparationError(f'{label} timed out after {timeout}s; check local trust prompts and retry interactively') from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPSPreparationError(f'{label} failed; retry interactively') from exc
    if result.returncode:
        hint = ' Run mkcert -install interactively in this environment.' if label == 'mkcert trust setup' else ''
        raise HTTPSPreparationError(f'{label} failed (exit {result.returncode}).{hint}')
    return result.stdout or ''


def _safe_file(path):
    if path.is_symlink():
        raise HTTPSPreparationError(f'Unsafe certificate path: {path.name} is a symlink')
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise HTTPSPreparationError(f'Unsafe certificate path: {path.name} is not a regular file')


def _safe_key(path):
    _safe_file(path)
    if path.exists():
        info = path.stat()
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise HTTPSPreparationError(f'Unsafe certificate key: {path.name} is owned by another user')
        if info.st_mode & 0o077:
            raise HTTPSPreparationError(f'Unsafe certificate key: {path.name} must have mode 0600')


def _san_identity(value, san_type=None):
    if san_type == 'DNS':
        return ('dns', value.lower().rstrip('.'))
    try:
        return ('ip', ipaddress.ip_address(value))
    except ValueError:
        return ('dns', value.lower().rstrip('.'))


@contextmanager
def _directory_lock(directory, timeout):
    lock = directory / '.https.lock'
    _safe_file(lock)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock, flags, 0o600)
        with os.fdopen(fd, 'r+') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (hasattr(os, 'geteuid') and info.st_uid != os.geteuid()):
                raise HTTPSPreparationError('Unsafe HTTPS lock file permissions or owner')
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise HTTPSPreparationError('Timed out waiting for local HTTPS preparation lock') from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise HTTPSPreparationError('Could not acquire local HTTPS preparation lock') from exc


@contextmanager
def _staging_directory(directory):
    staged_dir = Path(tempfile.mkdtemp(prefix='.https-', dir=directory))
    state = {'preserve': False}
    try:
        yield staged_dir, state
    finally:
        if not state['preserve']:
            shutil.rmtree(staged_dir)


def _valid_existing(material, names, runner, timeout, renewal_seconds):
    if not material.cert_file.is_file() or not material.key_file.is_file():
        return False
    try:
        def check(argv):
            return _invoke(runner, argv, timeout=timeout, interactive=False, label='certificate validation')
        check(['openssl', 'x509', '-in', str(material.cert_file), '-noout', '-checkend', str(renewal_seconds)])
        sans = check(['openssl', 'x509', '-in', str(material.cert_file), '-noout', '-ext', 'subjectAltName'])
        present = {_san_identity(value, kind) for kind, value in re.findall(r'(DNS|IP Address):([^,\s]+)', sans)}
        if not {_san_identity(name) for name in names}.issubset(present):
            return False
        cert_key = check(['openssl', 'x509', '-in', str(material.cert_file), '-noout', '-pubkey'])
        private_key = check(['openssl', 'pkey', '-in', str(material.key_file), '-pubout'])
        if cert_key.strip() != private_key.strip():
            return False
        check(['openssl', 'verify', '-CAfile', str(material.ca_file), str(material.cert_file)])
        return True
    except HTTPSPreparationError:
        return False


def prepare_local_https(cert_dir, hostname='localhost', *, extra_sans=('localhost', '127.0.0.1', '::1'),
                        runner=None, interactive=True, timeout=20, renewal_days=7):
    """Install local mkcert trust and return validated certificate paths.

    Invoke only for workflows that need HTTPS. This checks trust in the invoking
    environment; callers must independently check their host browser and clients.
    """
    runner = runner or _default_runner
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if renewal_days <= 0:
        raise ValueError('renewal_days must be positive')
    renewal_seconds = int(renewal_days * 86400)
    if renewal_seconds <= 0:
        raise ValueError('renewal_days is too small')
    names = tuple(dict.fromkeys((hostname, *extra_sans)))
    for name in names:
        if not name or name.startswith('-') or not re.fullmatch(r'[A-Za-z0-9.:-]+', name):
            raise ValueError('invalid certificate name')
    if '/' in hostname or '\\' in hostname or hostname in ('.', '..'):
        raise ValueError('invalid certificate hostname')
    if runner is _default_runner:
        from api_agent.runtime.mise_tools import ensure_tools
        try:
            ensure_tools('mkcert', timeout=timeout)
        except (OSError, TimeoutError) as exc:
            raise HTTPSPreparationError('Unable to prepare mkcert for local HTTPS') from exc
    for command in ('mkcert', 'openssl'):
        if runner is _default_runner and shutil.which(command) is None:
            raise HTTPSPreparationError(f'{command} is required for local HTTPS; install {command} and retry')
    directory = Path(cert_dir)
    if directory.exists() and not directory.is_dir():
        raise HTTPSPreparationError('Certificate directory is not a directory')
    directory.mkdir(parents=True, exist_ok=True)
    # Resolve aliases so cooperating callers lock the same inode and stage files
    # on the same filesystem while preserving the caller's symlink directory.
    actual_directory = directory.resolve()
    with _directory_lock(actual_directory, timeout):
        return _prepare_locked(directory, hostname, names, runner, interactive, timeout, renewal_seconds)


def _prepare_locked(directory, hostname, names, runner, interactive, timeout, renewal_seconds):
    cert = directory / f'{hostname}.pem'
    key = directory / f'{hostname}-key.pem'
    for path in (cert, key):
        _safe_file(path)
    _safe_key(key)
    _invoke(runner, ['mkcert', '-install'], timeout=timeout, interactive=interactive, label='mkcert trust setup')
    ca_root = _invoke(runner, ['mkcert', '-CAROOT'], timeout=timeout, interactive=False, label='mkcert CA lookup').strip()
    if not ca_root:
        raise HTTPSPreparationError('mkcert CA lookup returned no directory')
    ca = Path(ca_root) / 'rootCA.pem'
    if not ca.is_file():
        raise HTTPSPreparationError('mkcert public rootCA.pem is missing; run mkcert -install interactively')
    material = TLSMaterial(cert, key, ca)
    if _valid_existing(material, names, runner, timeout, renewal_seconds):
        return HTTPSPreparation(material, ('Local certificate reused.', 'Verify host browser trust separately if it runs outside this environment.'))
    with _staging_directory(directory) as (staged_dir, recovery):
        staged_cert, staged_key = staged_dir / 'cert.pem', staged_dir / 'key.pem'
        _invoke(runner, ['mkcert', '-cert-file', str(staged_cert), '-key-file', str(staged_key), *names],
                timeout=timeout, interactive=interactive, label='mkcert certificate generation')
        if not staged_cert.is_file() or not staged_key.is_file():
            raise HTTPSPreparationError('mkcert did not create certificate and key files')
        os.chmod(staged_key, 0o600)
        os.chmod(staged_cert, 0o644)
        if not _valid_existing(TLSMaterial(staged_cert, staged_key, ca), names, runner, timeout, renewal_seconds):
            raise HTTPSPreparationError('Generated certificate failed validation')
        _safe_file(cert)
        _safe_key(key)
        old_cert, old_key = staged_dir / 'old-cert.pem', staged_dir / 'old-key.pem'
        installed = {cert: False, key: False}
        try:
            if cert.exists():
                os.replace(cert, old_cert)
            if key.exists():
                os.replace(key, old_key)
            os.replace(staged_cert, cert)
            installed[cert] = True
            os.replace(staged_key, key)
            installed[key] = True
            if not _valid_existing(material, names, runner, timeout, renewal_seconds):
                raise HTTPSPreparationError('Installed certificate failed validation')
        except BaseException as exc:
            # Never leave a newly mixed pair available after a failed install.
            restore_failed = False
            for target, backup in ((cert, old_cert), (key, old_key)):
                # The backup may exist even if cancellation occurred just after
                # os.replace returned and before the stage flag was updated.
                if not backup.exists() and not installed[target]:
                    continue
                try:
                    if installed[target] and target.exists():
                        target.unlink()
                    if backup.exists():
                        os.replace(backup, target)
                except OSError:
                    restore_failed = True
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
            if restore_failed:
                recovery['preserve'] = True
                raise HTTPSPreparationError(
                    f'Could not restore certificate pair; recover backup files from {staged_dir}'
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise HTTPSPreparationError('Could not install a validated certificate pair; retry local HTTPS preparation') from exc
    return HTTPSPreparation(material, ('Local certificate generated.', 'Verify host browser trust separately if it runs outside this environment.'))
