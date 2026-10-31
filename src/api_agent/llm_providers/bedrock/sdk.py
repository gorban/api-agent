"""Bounded asynchronous process transport for synchronous Bedrock SDK work."""
import asyncio
import json
import os
import sys
import signal
import socket
from pathlib import Path

from api_agent.providers.contracts import ContractError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.progress import component_timeout
from api_agent.providers.control import finish_cleanup
from .worker import MAX_FRAME, ALL_OPERATIONS, json_value, DEFAULT_INFERENCE_TIMEOUT, inference_timeout


class SDKTransport:
    def __init__(self, timeout=DEFAULT_INFERENCE_TIMEOUT):
        self.timeout = inference_timeout(timeout)
        self._closed = False
        self._processes = set()
        self._launches = set()
        self._stops = {}
        self._closing = None

    async def _stop(self, process):
        # Active ownership is the admission receipt; a completed owner is inert.
        if process not in self._processes:
            return
        state = self._stops.setdefault(process, {'phase': 0, 'task': None})
        task = state['task']
        if (task is None or task.cancelled()
                or task.done() and task.exception() is not None):
            async def dispose():
                # Parent exit does not mean its credential-helper group exited.
                signals = (signal.SIGTERM, signal.SIGKILL)
                while state['phase'] < len(signals):
                    try:
                        os.killpg(process.pid, signals[state['phase']])
                    except ProcessLookupError:
                        # Never target this numeric group again after absence.
                        state['phase'] = len(signals)
                    else:
                        state['phase'] += 1
                await process.wait()
                self._processes.discard(process)
                self._stops.pop(process)
            task = state['task'] = asyncio.create_task(dispose())
        # A canceled cleanup waiter does not revoke the shared disposal owner.
        await asyncio.shield(task)

    async def _timed(self, work, budget):
        remaining = budget.remaining
        if remaining == 0:
            raise TimeoutError('Bedrock SDK operation deadline expired')
        async with component_timeout('Bedrock SDK operation', remaining):
            return await work()

    async def stream(self, operation, payload, *, auth, join=None, on_dispatch=None):
        if os.name != 'posix':
            raise SetupPrerequisiteError('Bedrock SDK process isolation currently requires POSIX.')
        if self._closed:
            raise SessionClosedError('Bedrock transport is closed')
        if operation not in ALL_OPERATIONS or not isinstance(payload, dict):
            raise ContractError('Unsupported Bedrock SDK request')
        try:
            json_value(payload)
        except (ValueError, TypeError, RecursionError):
            raise ContractError('Invalid Bedrock SDK JSON request or transport limit') from None
        owned = []
        channels = []
        authority_lease = None
        authority_values = None
        async with component_timeout('Bedrock SDK operation', self.timeout) as budget:
            pass
        try:
            owner = await self._timed(auth, budget)
            auth_config = dict(owner.worker_config())
            try:
                request = json.dumps(json_value({'operation': operation, 'payload': payload, 'auth': auth_config}), allow_nan=False).encode() + b'\n'
            except (ValueError, TypeError, RecursionError):
                raise ContractError('Invalid Bedrock SDK JSON request or transport limit') from None
            if len(request) > MAX_FRAME:
                raise ContractError('Bedrock request exceeds transport limit')
            environment = dict(os.environ)
            for key in tuple(environment):
                if key.startswith('OPENAI_') or key in {'AWS_BEARER_TOKEN_BEDROCK', 'AWS_BEDROCK_BASE_URL'}:
                    environment.pop(key)
            if auth_config['mode'] in {'access_key', 'temporary'}:
                environment.pop('AWS_SESSION_TOKEN', None)
                environment.pop('AWS_SECURITY_TOKEN', None)
            if join is not None:
                parent_channel, child_channel = socket.socketpair()
                parent_channel.setblocking(False)
                channels.extend((parent_channel, child_channel))
                environment["API_AGENT_BEDROCK_AUTHORITY_FD"] = str(child_channel.fileno())
            else:
                environment.pop("API_AGENT_BEDROCK_AUTHORITY_FD", None)
            async def launch():
                if self._closed:
                    raise SessionClosedError('Bedrock transport is closed')
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).with_name('worker.py')),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, env=environment, limit=MAX_FRAME,
                        start_new_session=True, pass_fds=(child_channel.fileno(),) if join is not None else ())
                except OSError:
                    raise ContractError('Unable to start Bedrock SDK worker') from None
                owned.append(process)
                self._processes.add(process)
            async def wait_launch():
                # Admission and ownership registration contain no suspension.
                if self._closed:
                    raise SessionClosedError('Bedrock transport is closed')
                task = asyncio.create_task(launch())
                self._launches.add(task)
                try:
                    await task
                finally:
                    self._launches.discard(task)
            await self._timed(lambda: finish_cleanup(wait_launch()), budget)
            process = owned[0]
            if join is not None:
                child_channel.close()
            if self._closed:
                raise SessionClosedError('Bedrock transport is closed')
            remaining = budget.remaining
            if remaining == 0:
                raise TimeoutError('Bedrock SDK operation deadline expired')
            wire_request = json.loads(request)
            wire_request['timeout'] = remaining
            request = json.dumps(wire_request, allow_nan=False).encode() + b'\n'
            if len(request) > MAX_FRAME:
                raise ContractError('Bedrock request exceeds transport limit')
            process.stdin.write(request)
            await self._timed(process.stdin.drain, budget)
            process.stdin.close()
            if join is not None:
                loop = asyncio.get_running_loop()
                raw_join = bytearray()
                while not raw_join.endswith(b'\n'):
                    part = await self._timed(lambda: loop.sock_recv(parent_channel, 32769 - len(raw_join)), budget)
                    if not part:
                        # An error before credential acquisition is reported on stdout.
                        break
                    raw_join.extend(part)
                    if len(raw_join) > 32768:
                        raise ContractError('Invalid Bedrock authority handoff')
                if raw_join:
                    try:
                        values = json.loads(raw_join)
                        if (type(values) is not list or len(values) > 64 or
                            any(type(value) is not str or not value for value in values) or
                            sum(len(value.encode()) for value in values) > 16384):
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        raise ContractError('Invalid Bedrock authority handoff') from None
                    joined = await self._timed(lambda: join(values), budget)
                    if joined is None:
                        union = values
                    else:
                        union, authority_lease = joined
                    try:
                        if (type(union) is not list or len(union) > 64 or
                            any(type(value) is not str or not value for value in union) or
                            sum(len(value.encode()) for value in union) > 16384 or
                            not set(values) <= set(union)):
                            raise ValueError()
                        encoded_union = json.dumps(union, ensure_ascii=False).encode() + b'\n'
                        if len(encoded_union) > 32768:
                            raise ValueError()
                    except (TypeError, ValueError, UnicodeError):
                        raise ContractError('Invalid Bedrock authority handoff') from None
                    await self._timed(lambda: loop.sock_sendall(parent_channel, encoded_union), budget)
                    ready = await self._timed(lambda: loop.sock_recv(parent_channel, 1), budget)
                    if ready != b'1':
                        raise ContractError('Bedrock worker rejected credential authority')
                    if on_dispatch is not None:
                        on_dispatch()
                    await self._timed(lambda: loop.sock_sendall(parent_channel, b'1'), budget)
                    authority_values = tuple(union)
                parent_channel.close()
            elif on_dispatch is not None:
                on_dispatch()
            while True:
                try:
                    raw = await self._timed(process.stdout.readline, budget)
                except ValueError:
                    raise ContractError('Invalid Bedrock worker response') from None
                if not raw:
                    break
                try:
                    frame = json_value(json.loads(raw))
                except (ValueError, UnicodeError):
                    raise ContractError('Invalid Bedrock worker response') from None
                if not isinstance(frame, dict):
                    raise ContractError('Invalid Bedrock worker response')
                if 'error' in frame:
                    kind = frame['error']
                    if kind == 'setup':
                        raise SetupPrerequisiteError('Install the optional Bedrock SDK dependencies.')
                    if kind not in {'authentication', 'authorization', 'rate_limited', 'unavailable', 'network', 'invalid_request', 'unknown'}:
                        raise ContractError('Invalid Bedrock worker error')
                    execution = frame.get('execution')
                    if execution not in {'not_started', 'possibly_executed'}:
                        raise ContractError('Invalid Bedrock worker execution state')
                    status = frame.get('http_status')
                    message = ('Bedrock network request failed; connection lost or timed out.' if kind == 'network'
                               else 'Bedrock SDK operation failed; verify provider setup and permissions.')
                    code = frame.get('code')
                    if code is not None:
                        from .mantle import RESPONSE_CODES
                        if (type(code) is not str or code not in RESPONSE_CODES
                                or kind != 'unknown' or execution != 'possibly_executed'):
                            raise ContractError('Invalid Bedrock worker response code')
                        message = ('Bedrock inference response could not be safely released.' if code == 'response_safety_rejected'
                                   else 'Bedrock inference response failed contract validation.')
                    validation_code = code
                    request_id = None
                    if 'aws_error_rejected' in frame and frame['aws_error_rejected'] is not True:
                        raise ContractError('Invalid Bedrock AWS diagnostic marker')
                    if frame.get('aws_error_rejected') is True:
                        message += ' (additional AWS diagnostic withheld)'
                    if 'aws_error' in frame:
                        if (authority_values is None or execution != 'possibly_executed'
                                or code is not None or 'aws_error_rejected' in frame):
                            raise ContractError('Unadmitted Bedrock AWS diagnostic')
                        from .runtime_worker import validate_aws_error
                        detail = validate_aws_error(frame['aws_error'], authority_values)
                        message, code = detail['message'], detail.get('code')
                        request_id = detail.get('request_id')
                    error = ProviderAccessError(kind, message, status=status, code=code, request_id=request_id)
                    if 'diagnostics' in frame:
                        from api_agent.telemetry.inference_diagnostics import valid_diagnostics
                        if validation_code is None or not valid_diagnostics(frame['diagnostics']):
                            raise ContractError('Invalid Bedrock worker diagnostics')
                        error.provider_diagnostics = frame['diagnostics']
                        from api_agent.workflows.inference_capture import capture_transport_diagnostics
                        capture_transport_diagnostics(frame['diagnostics'])
                    if 'usage' in frame:
                        from api_agent.providers.inference import result_from_wire
                        error.usage = result_from_wire({'text': '', 'outcome': 'completed', 'usage': frame['usage']}).usage
                    error.execution = execution
                    if 'provider_response' in frame:
                        if (type(frame['provider_response']) is not dict or join is None
                                or validation_code == 'response_safety_rejected'
                                or frame.get('aws_error_rejected') is True):
                            raise ContractError('Invalid Bedrock worker response')
                        error.provider_response = frame['provider_response']
                    hint = frame.get('retry_after')
                    if status is not None:
                        error.http_status = status
                    if hint is not None:
                        import math
                        if type(hint) not in (int, float) or not math.isfinite(hint) or hint < 0:
                            raise ContractError('Invalid Bedrock worker retry delay')
                        error.retry_after = hint
                    raise error
                if 'diagnostics' in frame:
                    from api_agent.telemetry.inference_diagnostics import valid_diagnostics
                    if set(frame) != {'result', 'diagnostics'} or not valid_diagnostics(frame['diagnostics']):
                        raise ContractError('Invalid Bedrock worker diagnostics')
                    from api_agent.workflows.inference_capture import capture_transport_diagnostics
                    capture_transport_diagnostics(frame.pop('diagnostics'))
                if set(frame) not in ({'event'}, {'result'}) or not isinstance(next(iter(frame.values())), dict):
                    raise ContractError('Invalid Bedrock worker response')
                yield next(iter(frame.values()))
            if await self._timed(process.wait, budget):
                raise ContractError('Bedrock worker failed')
        finally:
            for channel in channels:
                channel.close()
            for process in owned:
                await finish_cleanup(self._stop(process))
            if authority_lease is not None:
                authority_lease.release()

    async def call(self, operation, payload, *, auth, join=None, on_dispatch=None):
        results = [value async for value in self.stream(operation, payload, auth=auth, join=join, on_dispatch=on_dispatch)]
        if len(results) != 1:
            raise ContractError('Invalid Bedrock worker result count')
        return results[0]

    async def aclose(self):
        self._closed = True
        if (self._closing is None or self._closing.cancelled()
                or self._closing.done() and self._closing.exception() is not None):
            async def drain():
                # No new launches can pass admission after the closed gate.
                await asyncio.gather(*tuple(self._launches), return_exceptions=True)
                results = await asyncio.gather(
                    *(self._stop(process) for process in tuple(self._processes)),
                    return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    raise ContractError('Bedrock SDK cleanup failed; retry disposal.') from None
            self._closing = asyncio.create_task(drain())

        async def wait_close():
            await asyncio.shield(self._closing)
        await finish_cleanup(wait_close())
