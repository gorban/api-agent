"""Own bounded local schema workers without inheriting invocation credentials."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from .progress import report_timeout_sync

_WORKER = Path(__file__).with_name('schema_validation_worker.py')
_STARTUP_SECONDS = 5.0
_VALIDATION_SECONDS = 1.0
_MAX_BYTES = 2097152


def isolated_valid_schema(definition, value):
    return isolated_schema_verdict(definition, value) is True


def isolated_schema_verdict(definition, value):
    """Return true/false only for an observed verdict; unavailable is None."""
    try:
        raw = json.dumps([definition, value], allow_nan=False, ensure_ascii=True).encode('ascii')
        if len(raw) > _MAX_BYTES:
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    with report_timeout_sync('Local schema validation', _STARTUP_SECONDS + _VALIDATION_SECONDS) as budget:
        return _validate(raw, budget)


def _validate(raw, budget):
    process = None
    threads = []
    ready, output = queue.Queue(1), queue.Queue(1)
    try:
        if budget.remaining == 0:
            return None
        process = subprocess.Popen([sys.executable, '-I', str(_WORKER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        def read():
            try:
                ready.put(process.stdout.read(1))
                output.put(process.stdout.read(2))
            except (OSError, ValueError):
                ready.put_nowait(b'') if ready.empty() else None
                output.put_nowait(b'') if output.empty() else None
        reader = threading.Thread(target=read, daemon=True)
        threads.append(reader)
        reader.start()
        if ready.get(timeout=min(_STARTUP_SECONDS, budget.remaining)) != b'R':
            return None
        started = time.monotonic()
        def write():
            try:
                process.stdin.write(raw)
                process.stdin.close()
            except (OSError, ValueError):
                pass
        writer = threading.Thread(target=write, daemon=True)
        threads.append(writer)
        writer.start()
        timeout = min(_VALIDATION_SECONDS, budget.remaining)
        result = output.get(timeout=timeout)
        remaining = min(budget.remaining, max(0, timeout - (time.monotonic() - started)))
        process.wait(timeout=remaining)
        return result == b'1' if process.returncode == 0 and result in (b'0', b'1') else None
    except (OSError, queue.Empty, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
            for thread in threads:
                thread.join()
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass


async def async_isolated_valid_schema(definition, value):
    return await async_isolated_schema_verdict(definition, value) is True


async def async_isolated_schema_verdict(definition, value):
    """Validate without blocking sibling tasks; cancellation owns worker disposal."""
    import asyncio
    from .control import finish_cleanup
    from .progress import report_timeout
    try:
        raw = json.dumps([definition, value], allow_nan=False, ensure_ascii=True).encode('ascii')
        if len(raw) > _MAX_BYTES:
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    spawn = None
    async with report_timeout('Local schema validation', _STARTUP_SECONDS + _VALIDATION_SECONDS) as budget:
        try:
            if budget.remaining == 0:
                return None
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, '-I', str(_WORKER), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}, limit=16))
            started = time.monotonic()
            startup = min(_STARTUP_SECONDS, budget.remaining)
            process = await asyncio.wait_for(asyncio.shield(spawn), startup)
            remaining = min(budget.remaining, max(0, startup - (time.monotonic() - started)))
            if await asyncio.wait_for(process.stdout.readexactly(1), remaining) != b'R':
                return None
            async with report_timeout('Schema validation execution', min(_VALIDATION_SECONDS, budget.remaining) or 1e-9) as active:
                process.stdin.write(raw)
                await asyncio.wait_for(process.stdin.drain(), active.remaining)
                process.stdin.close()
                answer = await asyncio.wait_for(process.stdout.read(2), active.remaining)
                if answer not in (b'0', b'1'):
                    return None
                extra = await asyncio.wait_for(process.stdout.read(1), active.remaining)
                await asyncio.wait_for(process.wait(), active.remaining)
                return answer == b'1' if not extra and process.returncode == 0 else None
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None
        finally:
            if spawn is not None:
                await finish_cleanup(_async_dispose(spawn))


async def _async_dispose(spawn):
    try:
        process = await spawn
    except OSError:
        return
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.stdin.close()
    while await process.stdout.read(65536):
        pass
    await process.wait()
