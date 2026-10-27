"""Async owner of private sign-in IPC and native child process groups."""
import asyncio
import json
import os
from pathlib import Path
import sys

from api_agent.providers.contracts import ContractError, InteractionRequiredError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout
from api_agent.llm_providers.bedrock.sdk import SDKTransport
from api_agent.llm_providers.bedrock.login_worker import MAX_FRAME


_STAGES = frozenset({'credential_resolution', 'sign_in', 'credential_export',
                     'identity_verification', 'result_validation', 'worker_protocol'})
_REASONS = {'tls': 'unavailable', 'connectivity': 'unavailable',
            'malformed': 'invalid_request', 'unknown': 'unknown',
            'input_closed': 'invalid_request', 'input_limit': 'invalid_request',
            'input_malformed': 'invalid_request', 'input_multiline': 'invalid_request',
            'output_limit': 'invalid_request',
            'invalid_code': 'invalid_request', 'state_mismatch': 'authentication',
            'expired_code': 'authentication', 'permissions': 'authorization'}
_GUIDANCE = {
    'input_closed': ' The sign-in input channel closed. Restart sign-in through the launcher.',
    'input_limit': ' The input exceeded the sign-in limit. Copy only the current authorization code.',
    'input_malformed': ' The native sign-in worker received an invalid input frame. Report this fixed error without the authorization code.',
    'input_multiline': ' The authorization code must be one complete line. Copy only the code from the current browser sign-in.',
    'output_limit': ' The native output exceeded the sign-in limit. Report this fixed error without login URLs or authorization codes.',
    'invalid_code': ' Copy the complete authorization code from the current browser sign-in.',
    'state_mismatch': ' The authorization code belongs to a different sign-in attempt. Use the current browser sign-in.',
    'expired_code': ' The authorization code expired. Restart sign-in when ready.',
    'permissions': ' The account lacks permission to create sign-in credentials. Contact your account administrator.',
}


def acquisition_error(frame):
    if (set(frame) != {'error', 'stage', 'kind', 'reason'}
            or frame['error'] != 'acquisition' or type(frame['stage']) is not str
            or frame['stage'] not in _STAGES or type(frame['reason']) is not str
            or type(frame['kind']) is not str
            or not (frame['reason'] == 'rejected' and frame['kind'] in {'authentication', 'authorization'}
                    or frame['reason'] in _REASONS and frame['kind'] == _REASONS[frame['reason']])):
        raise ContractError('Invalid AWS sign-in failure response')
    error = ProviderAccessError(frame['kind'],
        'AWS sign-in failed. Stage: ' + frame['stage'] + '; category: ' + frame['kind'] + '; reason: ' + frame['reason'] + '.' + _GUIDANCE.get(frame['reason'], ''),
        code=frame['reason'])
    # This acquisition failed before inference, including an unknown cause.
    error.execution = 'not_started'
    return error


class LoginTransport(SDKTransport):
    async def acquire(self, config, environment, interaction=None):
        if os.name != 'posix':
            raise SetupPrerequisiteError('AWS sign-in currently requires POSIX process isolation.')
        owned = []
        reader = None
        try:
            async with component_timeout('AWS sign-in', self.timeout):
                if self._closed:
                    raise SessionClosedError('AWS sign-in transport is closed')
                caller = asyncio.current_task()
                async def launch():
                    if config.get('login'):
                        check = getattr(interaction, 'check_ready', None)
                        if not callable(check):
                            raise InteractionRequiredError('AWS sign-in requires a visible interaction owner.')
                        try:
                            await check()
                        except InteractionRequiredError:
                            raise
                        except Exception:
                            raise InteractionRequiredError('AWS sign-in interaction is unavailable.') from None
                    if self._closed:
                        raise SessionClosedError('AWS sign-in transport is closed')
                    if caller.cancelling():
                        raise asyncio.CancelledError
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).with_name('login_worker.py')),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=environment,
                        limit=MAX_FRAME, start_new_session=True, umask=0o077)
                    owned.append(process)
                    self._processes.add(process)
                task = asyncio.create_task(launch())
                self._launches.add(task)
                try:
                    async def drain_launch():
                        await task
                    await finish_cleanup(drain_launch())
                finally:
                    self._launches.discard(task)
                process = owned[0]
                if self._closed:
                    raise SessionClosedError('AWS sign-in transport is closed')
                async def send(value):
                    raw = json.dumps(value, allow_nan=False).encode() + b'\n'
                    if len(raw) > MAX_FRAME:
                        raise ContractError('AWS sign-in transport limit exceeded')
                    process.stdin.write(raw)
                    await process.stdin.drain()
                await send(config)
                async def replies():
                    while True:
                        value = await interaction.read()
                        if value is not None:
                            if config['method'] != 'direct':
                                raise ContractError('AWS device sign-in does not accept terminal input.')
                            await send({'input': value})
                        await asyncio.sleep(0)
                if config['login']:
                    reader = asyncio.create_task(replies())
                result = None
                while True:
                    frame_task = asyncio.create_task(process.stdout.readline())
                    try:
                        if reader is not None:
                            done, _ = await asyncio.wait((frame_task, reader), return_when=asyncio.FIRST_COMPLETED)
                            if reader in done:
                                await reader
                        raw = await frame_task
                    finally:
                        if not frame_task.done():
                            frame_task.cancel()
                            await asyncio.gather(frame_task, return_exceptions=True)
                    if not raw:
                        break
                    if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                        raise ContractError('Invalid AWS sign-in response')
                    frame = json.loads(raw)
                    if type(frame) is not dict:
                        raise ContractError('Invalid AWS sign-in response')
                    if set(frame) == {'auth'} and reader is not None and type(frame['auth']) is str:
                        await interaction.write(frame['auth'])
                    elif frame == {'auth_done': True} and reader is not None:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
                        reader = None
                    elif set(frame) == {'result'} and reader is None and result is None and type(frame['result']) is dict:
                        result = frame['result']
                    elif frame.get('error') == 'acquisition':
                        raise acquisition_error(frame)
                    elif frame == {'error': 'setup'}:
                        raise SetupPrerequisiteError('Install AWS CLI >=2.32 and the optional Bedrock SDK dependencies.')
                    else:
                        raise ContractError('AWS sign-in failed; verify configuration and complete the visible handoff.')
                if await process.wait() or result is None:
                    raise ContractError('AWS sign-in worker failed')
                return result
        except (ContractError, InteractionRequiredError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError):
            raise
        except (ValueError, UnicodeError, OSError):
            raise ContractError('Invalid AWS sign-in worker response or process failure') from None
        finally:
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            for process in owned:
                await finish_cleanup(self._stop(process))
