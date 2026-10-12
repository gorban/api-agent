"""Optional native quota-client preparation at execution time only.

The application connection is used only for its verified email. Its OAuth
credentials never enter the native client's process or sign-in protocol.
"""
import asyncio
from hashlib import sha256
import json
import os
import platform
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from urllib.parse import urlsplit
from uuid import uuid4

from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout
from api_agent.providers.sessions import wait_browser_result
from api_agent.telemetry.usage import QuotaReadError
from api_agent.runtime.tasks import offload

from .quota_client import _close, _matches, _rpc, native_environment


_CODEX_VERSION = '0.160.1'
_CODEX_INTEGRITY = 'sha512-f1yrJhwgimKQI1kYQlxdPJcFwkNZxZrbz7Hf89EAcnLqkQ7TiESzr2FgSZn13Ga5RuHjlVUfurzwq10wk9zw2g=='
_REGISTRY = 'https://registry.npmjs.org/'
_NATIVE_INTEGRITY = {
    '@openai/codex-linux-x64': 'sha512-sIDhqV+bsZKKVaVFVY5iB+pAzyOz2XRm3H1KXCsSJwGj5p98qnrT0Fweo1Hfe7WnyTp8mfBNdbVzUeO+mHYugA==',
    '@openai/codex-linux-arm64': 'sha512-JLyjBlmjPvwTaicHemw+y5xQSz3Uja2r7F/xkvS8gFufTA2g132Ak+0fo35xnJ/k2uc9fTtjm0LrsEGI78L6Ng==',
    '@openai/codex-darwin-x64': 'sha512-Q33hmY6BxhoQRgCo4f/xS20yMxORnilQMNdr5w1bXSkXGYAty8/z9odcHbsNYUM6m0fNle12DHLQ5F0Dpfji3A==',
    '@openai/codex-darwin-arm64': 'sha512-6Znu97O7+Ie/cnUpsWLx6NlcPc7DNuj6sAs58xpt0/tKRgx4+xgE9zca1YrwqQUBjUQO1HexudyimmhpZjdWlA==',
}


class NativeClientSetupError(OSError):
    """Content-free optional setup failure, safe for launcher diagnostics."""


def _managed_cache():
    if platform.system() == 'Darwin':
        return Path.home() / 'Library' / 'Caches' / 'api-agent' / 'native-codex'
    root = os.environ.get('XDG_CACHE_HOME')
    return (Path(root) if root and Path(root).is_absolute() else Path.home() / '.cache') / 'api-agent' / 'native-codex'


def _platform_package():
    system = {'Linux': 'linux', 'Darwin': 'darwin'}.get(platform.system())
    machine = {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    return f'@openai/codex-{system}-{machine}' if system and machine else None


def _content_fingerprint(root, package, *, cancelled=None):
    entries = []
    count = total_size = 0
    # The parent is the managed cache boundary. Neither it nor any internal
    # directory may redirect package reads outside that boundary.
    parent = root.parent
    parent_info = parent.lstat()
    if (parent.is_symlink() or not parent.is_dir()
            or (hasattr(os, 'getuid') and parent_info.st_uid != os.getuid())
            or parent_info.st_mode & 0o022):
        raise ValueError('Untrusted native client cache')
    def paths():
        yield root
        yield root / 'package-lock.json'
        yield root / 'node_modules'
        yield root / 'node_modules' / '@openai'
        for directory in (root / 'node_modules' / '@openai' / 'codex', root / 'node_modules' / package):
            yield directory
            yield from directory.rglob('*')
    for path in paths():
        if cancelled is not None and cancelled():
            raise ValueError('Native client validation cancelled')
        count += 1
        if count > 1000:
            raise ValueError('Native client has too many files')
        info = path.lstat()
        if path.is_symlink() or (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or info.st_mode & 0o022:
            raise ValueError('Untrusted native client files')
        relative = str(path.relative_to(root))
        if path.is_dir():
            value = b'directory'
        elif path.is_file() and info.st_nlink == 1:
            total_size += info.st_size
            if info.st_size > 256 * 1024 * 1024 or total_size > 512 * 1024 * 1024:
                raise ValueError('Native client files exceed size limit')
            content = sha256()
            with path.open('rb') as handle:
                while chunk := handle.read(1024 * 1024):
                    if cancelled is not None and cancelled():
                        raise ValueError('Native client validation cancelled')
                    content.update(chunk)
            value = b'file:' + content.digest()
        else:
            raise ValueError('Untrusted native client file type')
        entries.append((relative, value))
    digest = sha256()
    for relative, value in sorted(entries):
        digest.update(relative.encode() + b'\0' + value + b'\0')
    return digest.hexdigest()


def _seal_install(root, package, *, cancelled=None):
    fingerprint = _content_fingerprint(root, package, cancelled=cancelled)
    marker = root / 'validation.json'
    marker.write_text(json.dumps({'version': _CODEX_VERSION, 'package': package,
                                  'fingerprint': fingerprint}, separators=(',', ':')))
    marker.chmod(0o600)


def _verify_install(root, package, *, sealed=True, cancelled=None):
    try:
        lock = json.loads((root / 'package-lock.json').read_text())
        entries = lock['packages']
        primary = entries['node_modules/@openai/codex']
        native = entries[f'node_modules/{package}']
        for entry in (primary, native):
            if not entry['resolved'].startswith(_REGISTRY):
                return None
        if primary['version'] != _CODEX_VERSION or primary['integrity'] != _CODEX_INTEGRITY:
            return None
        if native['version'] != _CODEX_VERSION + '-' + package.removeprefix('@openai/codex-') or native['integrity'] != _NATIVE_INTEGRITY[package]:
            return None
        if not (root / 'node_modules' / package / 'package.json').is_file():
            return None
        script = root / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
        if not script.is_file() or not os.access(script, os.X_OK):
            return None
        fingerprint = _content_fingerprint(root, package, cancelled=cancelled)
        if sealed:
            marker = root / 'validation.json'
            if marker.is_symlink() or marker.stat().st_mode & 0o077:
                return None
            if json.loads(marker.read_text()) != {'version': _CODEX_VERSION, 'package': package,
                                                   'fingerprint': fingerprint}:
                return None
        return script
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def _command(*args, timeout, env, cwd=None):
    starting = asyncio.create_task(asyncio.create_subprocess_exec(
        *map(str, args), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env, cwd=cwd, limit=65536))
    try:
        async with component_timeout('GPT quota client installation', timeout):
            process = await asyncio.shield(starting)
            chunks = []
            size = 0
            while chunk := await process.stdout.read(4096):
                size += len(chunk)
                if size > 8192:
                    raise NativeClientSetupError('Native quota client command output exceeded its limit.')
                chunks.append(chunk)
            if await process.wait() != 0:
                raise NativeClientSetupError('Native quota client setup failed.')
            return b''.join(chunks).decode('utf-8', errors='replace').strip()
    finally:
        await finish_cleanup(_close(starting))


async def install_native_client(*, cache_dir=None):
    """Install one pinned public CLI in an owned cache, with no global changes."""
    package = _platform_package()
    if package is None:
        raise NativeClientSetupError('Automatic native quota setup supports Linux and macOS; use an existing signed-in Codex CLI on this host.')
    node, npm = shutil.which('node'), shutil.which('npm')
    if node is None or npm is None:
        raise NativeClientSetupError('Automatic native quota setup needs Node.js/npm.')
    root = Path(cache_dir) if cache_dir is not None else _managed_cache()
    target = root / _CODEX_VERSION
    env = native_environment()
    env['npm_config_cache'] = str(root / 'npm-cache')
    cancellation = Event()
    script = await offload(_verify_install, target, package, cancellation=cancellation,
                           cancelled=cancellation.is_set)
    if script is not None:
        version = await _command(node, script, '--version', timeout=15, env=env)
        if version == f'codex-cli {_CODEX_VERSION}':
            return str(script)
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='install-', dir=root) as staging:
        stage = Path(staging)
        empty_config = stage / 'empty.npmrc'
        empty_config.write_text('')
        empty_config.chmod(0o600)
        env['npm_config_userconfig'] = str(empty_config)
        env['npm_config_globalconfig'] = str(empty_config)
        await _command(npm, 'install', '--prefix', stage, '--registry', _REGISTRY,
                       '--ignore-scripts', '--no-audit', '--no-fund', '--save-exact',
                       f'@openai/codex@{_CODEX_VERSION}', timeout=180, env=env, cwd=stage)
        cancellation = Event()
        script = await offload(_verify_install, stage, package, sealed=False,
                               cancellation=cancellation, cancelled=cancellation.is_set)
        if script is None:
            raise NativeClientSetupError('Native quota client package verification failed.')
        cancellation = Event()
        await offload(_seal_install, stage, package, cancellation=cancellation,
                      cancelled=cancellation.is_set)
        version = await _command(node, script, '--version', timeout=15, env=env)
        if version != f'codex-cli {_CODEX_VERSION}':
            raise NativeClientSetupError('Native quota client version verification failed.')
        if target.exists():
            # Never remove a client another owner may already be using.
            target = root / f'{_CODEX_VERSION}-{uuid4().hex}'
        stage.rename(target)
    return str(target / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js')


def _safe_auth_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == 'https' and parts.hostname in {'auth.openai.com', 'chatgpt.com'}
                and parts.username is None and parts.password is None and parts.port in (None, 443)
                and not parts.fragment)
    except ValueError:
        return False


async def _completed(process, login_id):
    for _ in range(256):
        raw = await process.stdout.readline()
        if not raw or len(raw) > 65536:
            raise ValueError('Invalid native login frame')
        frame = json.loads(raw)
        if type(frame) is not dict:
            raise ValueError('Invalid native login frame')
        if frame.get('method') != 'account/login/completed':
            continue
        params = frame.get('params')
        if type(params) is not dict or params.get('loginId') != login_id:
            continue
        return params.get('success') is True
    raise ValueError('Too many native login frames')


async def prepare_quota_client(connection, *, interactive, executable=None,
                               install_client=None, present_login=None):
    """Return a client path or safe error; login requires attended presentation.

    This is for an execution path that needs quota. Read-only pricing, history,
    and :func:`read_quota` must not call it.
    """
    email = getattr(connection, 'email', None)
    if not isinstance(email, str) or not email.strip():
        return QuotaReadError('identity')
    executable = executable or shutil.which('codex')
    if executable is None:
        try:
            executable = await (install_client or install_native_client)()
        except (OSError, ValueError, TimeoutError):
            return QuotaReadError('source')
    if not isinstance(executable, str) or not executable:
        return QuotaReadError('source')

    starting = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, 'app-server', '--listen', 'stdio://',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=native_environment(), limit=65536))
    try:
        async with component_timeout('GPT quota client preparation', 120):
            process = await asyncio.shield(starting)
            await _rpc(process, 1, 'initialize', {'clientInfo': {'name': 'api_agent_usage', 'version': '1'}})
            process.stdin.write(b'{"method":"initialized"}\n')
            await process.stdin.drain()
            account = await _rpc(process, 2, 'account/read', {'refreshToken': False})
            if _matches(account, email):
                return executable
            if 'account' not in account or account['account'] is not None:
                return QuotaReadError('identity')
            if not interactive or present_login is None:
                return QuotaReadError('source')
            login = await _rpc(process, 3, 'account/login/start', {'type': 'chatgpt'})
            login_id = login.get('loginId')
            url = login.get('authUrl')
            if login.get('type') != 'chatgpt' or not isinstance(login_id, str) or not login_id or not _safe_auth_url(url):
                return QuotaReadError('request')
            try:
                session = await present_login(url)
                if session is None:
                    completed = await _completed(process, login_id)
                else:
                    waiting = asyncio.create_task(_completed(process, login_id))
                    try:
                        completed = await wait_browser_result(waiting, session)
                    finally:
                        waiting.cancel()
                        async def drain():
                            await asyncio.gather(waiting, return_exceptions=True)
                        await finish_cleanup(drain())
            except BaseException as error:
                async def cancel_login():
                    try:
                        async with asyncio.timeout(3):
                            await _rpc(process, 4, 'account/login/cancel', {'loginId': login_id})
                    except (OSError, ValueError, TimeoutError):
                        pass
                await finish_cleanup(cancel_login())
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return QuotaReadError('request')
            if not completed:
                return QuotaReadError('source')
            account = await _rpc(process, 5, 'account/read', {'refreshToken': False})
            return executable if _matches(account, email) else QuotaReadError('identity')
    except (OSError, ValueError, TimeoutError):
        return QuotaReadError('request')
    finally:
        await finish_cleanup(_close(starting))
