"""Read optional local Codex quota without transferring application credentials."""
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import shutil
from uuid import uuid4

from api_agent.providers.progress import component_timeout
from api_agent.runtime.tasks import settle
from api_agent.providers.control import finish_cleanup
from api_agent.providers.costs import period_seconds
from api_agent.telemetry.usage import QuotaReadError, QuotaSnapshot


_NATIVE_ENV_KEYS = (
    'PATH', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT', 'WINDIR',
    'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'LC_CTYPE', 'XDG_CONFIG_HOME',
    'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'CODEX_HOME',
)


def native_environment():
    """Pass only native-client runtime state, never application credentials."""
    return {key: os.environ[key] for key in _NATIVE_ENV_KEYS if key in os.environ}


async def _rpc(process, identifier, method, params=None):
    frame = {'id': identifier, 'method': method}
    if params is not None:
        frame['params'] = params
    process.stdin.write((json.dumps(frame) + '\n').encode())
    await process.stdin.drain()
    for _ in range(16):
        raw = await process.stdout.readline()
        if not raw or len(raw) > 65536:
            raise ValueError('Invalid quota frame')
        data = json.loads(raw)
        if type(data) is not dict:
            raise ValueError('Invalid quota frame')
        if data.get('id') == identifier:
            if type(data.get('result')) is not dict or 'error' in data:
                raise ValueError('Quota request rejected')
            return data['result']
    raise ValueError('Too many quota frames')


def _matches(data, email):
    account = data.get('account')
    return (type(account) is dict and account.get('type') == 'chatgpt'
            and type(account.get('email')) is str
            and account['email'].strip().casefold() == email.strip().casefold())


def _snapshot(data, subject, period, now):
    buckets = data.get('rateLimitsByLimitId')
    bucket = buckets.get('codex') if type(buckets) is dict else data.get('rateLimits')
    if type(bucket) is not dict or bucket.get('limitId') != 'codex':
        return QuotaReadError('window')
    minutes = period_seconds(period, now=now) // 60
    windows = [value for key in ('primary', 'secondary')
               if type(value := bucket.get(key)) is dict and value.get('windowDurationMins') == minutes]
    if len(windows) != 1:
        return QuotaReadError('window')
    value = windows[0]
    percent, reset = value.get('usedPercent'), value.get('resetsAt')
    # The local client's protocol specifies int32 percentage points, not a
    # fractional reading. Preserve that one-point quantization for calibration.
    if (type(percent) is not int or not 0 <= percent <= 100
            or type(reset) is not int or not now.timestamp() < reset <= 253402300799):
        return QuotaReadError('response')
    meter = f'codex:{period}'
    account = sha256(subject.encode()).hexdigest()[:32]
    allocation_id = sha256(json.dumps(['api-agent/quota-allocation/v1', meter, subject], separators=(',', ':')).encode()).hexdigest()
    return QuotaSnapshot(percent, f'codex:{account}:{minutes}:{reset}',
                         datetime.fromtimestamp(reset, timezone.utc), now, pct_step=1,
                         allocation_id=allocation_id, observation_id=str(uuid4()))


async def _close(starting):
    try:
        process = await settle(starting)
    except Exception:
        return
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


async def read_quota(connection, *, period, executable=None):
    """Use only a matching, already signed-in client; never initiate login."""
    executable = executable or shutil.which('codex')
    if not executable or not connection.email.strip():
        return QuotaReadError('source')
    env = native_environment()
    starting = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, 'app-server', '--listen', 'stdio://',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, limit=65536))
    try:
        async with component_timeout('GPT subscription quota', 20):
            process = await asyncio.shield(starting)
            await _rpc(process, 1, 'initialize', {'clientInfo': {'name': 'api_agent_usage', 'version': '1'}})
            process.stdin.write(b'{"method":"initialized"}\n')
            await process.stdin.drain()
            before = await _rpc(process, 2, 'account/read', {'refreshToken': False})
            if not _matches(before, connection.email):
                return QuotaReadError('source')
            quota = await _rpc(process, 3, 'account/rateLimits/read')
            after = await _rpc(process, 4, 'account/read', {'refreshToken': False})
            if before != after or not _matches(after, connection.email):
                return QuotaReadError('source')
            return _snapshot(quota, connection.subject, period, datetime.now(timezone.utc))
    except (OSError, ValueError, TimeoutError):
        return QuotaReadError('request')
    finally:
        # Use a fresh cleanup budget, independent of the exhausted RPC deadline.
        await finish_cleanup(_close(starting))
