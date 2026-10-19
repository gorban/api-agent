"""Own a buffered reasoning request and its existing application lease heartbeats."""
import asyncio

from api_agent.providers.progress import component_timeout
from api_agent.providers.control import _cancel_once
from api_agent.runtime.tasks import settle
from api_agent.testing.storage import _Cleanup
from api_agent.testing.suite_lock import _cleanup_receipts

_INTERVAL = 10
_TERMINAL = {'completed', 'failed', 'cancelled', 'cleanup_pending'}


def _spawn(work):
    try:
        return asyncio.create_task(work)
    except BaseException:
        work.close()
        raise


async def _drain(tasks):
    requested = set()
    for task in tasks:
        if not task.done():
            _cancel_once(task, requested)
    drain = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.shield(drain)
    except asyncio.CancelledError as error:
        await settle(drain)
        return error


def _select_error(primary, interrupted):
    if interrupted is not None and (primary is None or isinstance(primary, Exception)):
        if primary is not None:
            interrupted.operation_error = primary
        return interrupted
    return primary


def _retain_receipts(error, *sources):
    receipts = tuple({id(receipt): receipt for source in (error, *sources)
                      for receipt in _cleanup_receipts(source)}.values())
    if not receipts:
        return None
    pending = receipts[0]
    for following in receipts[1:]:
        _Cleanup._append(pending, following)
    if error is not None:
        if error is not pending:
            error.cleanup_pending = pending
        owners = (*getattr(error, 'retained_owners', ()),
                  *(owner for source in sources for owner in getattr(source, 'retained_owners', ())),
                  *(owner for receipt in receipts for owner in receipt.owners))
        error.retained_owners = tuple({id(owner): owner for owner in owners}.values())
    return pending


async def post_chat(client, endpoint, nonce, body):
    """Renew at the documented ten-second interval; never extend the request deadline."""
    headers = {'Origin': 'http://127.0.0.1:8080', 'X-API-Agent-Session': nonce}
    identifier = body['session_id']
    failures = []

    async def send(path, value):
        try:
            return await client.post(endpoint+path, headers=headers, json=value)
        except BaseException as error:
            # Capture ownership before timeout/gather can reconstruct cancellation.
            failures.append(error)
            raise

    async def chat():
        return await send('/app/agent/chat', body)

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(_INTERVAL)
                async with component_timeout('Reasoning comparison heartbeat', 10):
                    response = await send('/app/agent/chat/heartbeat', {'session_id': identifier})
                    response.raise_for_status()
                    value = response.json()
                    if (type(value) is not dict or set(value) != {'session_id', 'status'}
                            or value['session_id'] != identifier
                            or type(value['status']) is not str
                            or value['status'] not in {'unknown', 'running'} | _TERMINAL):
                        raise ValueError('Invalid reasoning heartbeat acknowledgement')
                    if value['status'] in _TERMINAL:
                        return
        except BaseException as error:
            failures.append(error)
            raise

    tasks = []
    primary = None
    try:
        request = _spawn(chat())
        tasks.append(request)
        lease = _spawn(heartbeat())
        tasks.append(lease)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if request.done():
            return request.result()
        lease.result()
        return await request
    except BaseException as error:
        primary = error
        raise
    finally:
        interrupted = await _drain(tasks)
        selected = _select_error(primary, interrupted)
        pending = _retain_receipts(selected, primary, interrupted, *failures)
        if selected is not None and selected is not primary:
            raise selected
        if primary is None and pending is not None:
            raise pending
