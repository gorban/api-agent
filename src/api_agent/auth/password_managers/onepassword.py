"""1Password command creation, CSV fields and raw OTP parsing."""
import csv
import io
import os

from api_agent.auth.managers import ManagerError

version = 1


def command(source, kind, cli):
    argv = [cli, 'item', 'get', source.user, '--vault', source.vault]
    if kind == 'password':
        return argv + ['--fields', 'password', '--reveal']
    if kind == 'username':
        return argv + ['--fields', 'username']
    if kind == 'totp':
        return argv + ['--otp']
    raise ManagerError('Unsupported credential kind.')


def parse(output, kind):
    if kind in ('username', 'password'):
        try:
            rows = list(csv.reader(io.StringIO(output), strict=True))
        except csv.Error as error:
            raise ManagerError(f'1Password returned invalid {kind} CSV.') from error
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ManagerError(f'1Password {kind} output must contain exactly one CSV cell.')
        return rows[0][0]
    if kind != 'totp':
        raise ManagerError('Unsupported credential kind.')
    terminator = '\r\n' if os.name == 'nt' else '\n'
    return output[:-len(terminator)] if output.endswith(terminator) else output
