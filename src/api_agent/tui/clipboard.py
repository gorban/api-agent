"""Clipboard access through optional native tools and terminal copy support."""
import asyncio
import os
import shutil
import subprocess
import sys

from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout


class ClipboardError(RuntimeError):
    pass


def clear_local_clipboard(app):
    """Clear Textual's memory cache without emitting a terminal clipboard write."""
    app._clipboard = ''


def native_command():
    if sys.platform == 'darwin':
        name = 'pbcopy'
    elif sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY'):
        name = 'wl-copy'
    else:
        return None
    executable = shutil.which(name)
    return [executable] if executable else None


async def copy_text(app, text):
    """Never put copied values in arguments, diagnostics or subprocess output."""
    command = native_command()
    native = False
    if command:
        try:
            await asyncio.to_thread(subprocess.run, command, input=text.encode('utf-8'),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=2, check=True)
            native = True
        except (OSError, subprocess.SubprocessError):
            pass  # The terminal protocol remains available without native helpers.
    try:
        app.copy_to_clipboard(text)  # Also updates Textual's in-app paste buffer.
    except Exception:
        if not native:
            raise ClipboardError('Could not send the value to the clipboard.') from None
    return native


def native_read_command():
    """Select an optional native reader without consulting the in-app buffer."""
    if sys.platform == 'darwin':
        name, arguments = 'pbpaste', []
    elif sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY'):
        name, arguments = 'wl-paste', ['--no-newline']
    else:
        return None
    executable = shutil.which(name)
    return [executable, *arguments] if executable else None


async def read_text():
    """Read only on an explicit paste action; never use Textual's cached value."""
    guidance = 'Could not read the clipboard. Use terminal Paste to insert the value.'
    command = native_read_command()
    if not command:
        raise ClipboardError(guidance)
    spawn = None
    try:
        async with component_timeout('Clipboard read', 2):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=65537))
            process = await asyncio.shield(spawn)
            data = bytearray()
            while True:
                chunk = await process.stdout.read(65537 - len(data))
                data.extend(chunk)
                if len(data) > 65536:
                    raise ClipboardError(guidance)
                if not chunk:
                    break
            if await process.wait():
                raise ClipboardError(guidance)
            return data.decode('utf-8', errors='strict')
    except (OSError, UnicodeError, TimeoutError):
        raise ClipboardError(guidance) from None
    finally:
        if spawn is not None:
            await finish_cleanup(_dispose_reader(spawn))


async def _dispose_reader(spawn):
    try:
        process = await spawn
    except OSError:
        return
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
