"""Keeper command creation and raw single-terminator credential parsing."""
import os

from api_agent.auth.managers import ManagerError

version = 1


def command(source, kind, cli):
    item = f'{source.vault}/{source.user}'
    if kind == 'password':
        return [cli, 'find-password', item]
    if kind == 'username':
        return [cli, 'clipboard-copy', item, '--field', 'login', '--output', 'stdout']
    if kind == 'totp':
        return [cli, 'clipboard-copy', item, '--totp', '--output', 'stdout', '-t']
    raise ManagerError('Unsupported credential kind.')


def parse(output, kind):
    if kind not in ('username', 'password', 'totp'):
        raise ManagerError('Unsupported credential kind.')
    terminator = '\r\n' if os.name == 'nt' else '\n'
    return output[:-len(terminator)] if output.endswith(terminator) else output


def classify_login(stderr):
    """Recognize Keeper authentication failures, never general lookup errors."""
    import re
    return bool(re.search(
        r'persistent login is not working in this non-interactive environment'
        r'|persistent[ -]login.{0,120}(?:fail|expired|invalid|not available|not enabled)'
        r'|(?:fail|unable).{0,80}persistent[ -]login'
        r'|(?:not logged in|login required|please log(?:in| in))'
        r'|(?:^|\n)\s*(?:enter (?:your )?(?:master )?password|password\s*:)',
        stderr, re.IGNORECASE))
