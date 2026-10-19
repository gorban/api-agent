"""Confirm a multi-test browser inspection before execution resources are acquired."""

import sys


def debug_warning(mode, debug, tests, *, e2e_debug=False):
    """Describe the risk for distinct selected integration or E2E tests."""
    legacy = mode == 'e2e' and e2e_debug is True
    if mode not in {'integration', 'e2e'} or debug is not True and not legacy:
        return None
    count = len({test.id for test in tests})
    if count <= 1:
        return None
    setting = 'E2E_DEBUG=True' if legacy and debug is not True else 'DEBUG=True'
    disabled = 'DEBUG=False and E2E_DEBUG=False' if legacy else 'DEBUG=False'
    return (f'{setting} with {count} selected tests could open multiple browser windows '
            f'(up to {count}). To avoid this confirmation, set {disabled} and do not pass --debug. '
            'Enter literal y to continue; every other response cancels.')


def confirm_terminal_debug(warning, *, stdin=None, stdout=None):
    """Use the same explicit terminal response policy as worktree approval."""
    if warning is None:
        return True
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stdout.write(warning + '\nContinue? [y/N] (default No): ')
    stdout.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError, AttributeError):
        return False
    return answer.endswith('\n') and answer.rstrip('\r\n') == 'y'
