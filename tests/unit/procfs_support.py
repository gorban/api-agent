"""Race-free reads of Linux process state for tests that wait on reaping.

This is a flat support module, not part of a ``helpers`` package: workflow tests
declare a native module named ``helpers``, and a same-named test package already
imported in a worker would make their dependency admission refuse it.
"""
from pathlib import Path


def process_state(pid):
    """Return the one-letter state of ``pid``, or ``None`` once the task is gone.

    ``Path.read_text`` opens and then reads ``/proc/<pid>/stat``. A task reaped
    between those two system calls makes the open succeed and the read fail with
    ``ProcessLookupError``; a task reaped earlier fails the open with
    ``FileNotFoundError``. Both mean "gone". Any other ``OSError`` stays loud.
    """
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The command name may contain spaces and parentheses; the state letter
    # follows the last closing parenthesis.
    return text.rsplit(')', 1)[1].split()[0]


def is_gone_or_zombie(pid):
    """True once the task is reaped or only awaiting its parent's wait."""
    return process_state(pid) in (None, 'Z')
