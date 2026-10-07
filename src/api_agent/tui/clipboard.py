"""Clipboard writes through optional native tools and Textual's terminal protocol."""
import asyncio
import os
import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
    pass


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
