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


def login_probe(source, cli):
    """Recover CLI authentication without an unmasked record command in the PTY."""
    return [cli, 'whoami']


def _json_rows(output):
    import json
    try:
        rows = json.loads(output)
    except (ValueError, TypeError):
        raise ManagerError('Keeper selected-login output is invalid.') from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ManagerError('Keeper selected-login output is invalid.')
    return rows


def _single(fields, name):
    values = [row.get('value') for row in fields if row.get('name', '').casefold() == name]
    if len(values) > 1:
        raise ManagerError('Keeper selected-login fields are ambiguous.')
    value = values[0] if values else None
    if isinstance(value, list):
        if len(value) > 1:
            raise ManagerError('Keeper selected-login fields are ambiguous.')
        value = value[0] if value else None
    return None if value == '' else value


def _passkey(value, username, relying_party_id):
    from api_agent.auth.login_credentials import PasskeyCredential, decode_id
    from cryptography.hazmat.primitives.asymmetric import ec
    if not isinstance(value, dict):
        raise ManagerError('Keeper passkey is invalid.')
    if value.get('username') != username or value.get('relyingParty') != relying_party_id:
        raise ManagerError('Keeper passkey does not match the selected account and relying party.')
    count = value.get('signCount')
    if type(count) is not int or not 0 <= count <= 0xffffffff:
        raise ManagerError('Keeper passkey counter is invalid.')
    decode_id(value.get('credentialId'), maximum=1023)
    decode_id(value.get('userId'), maximum=64)
    key = value.get('privateKey')
    if (not isinstance(key, dict) or key.get('kty') != 'EC' or key.get('crv') != 'P-256'
            or key.get('alg', 'ES256') != 'ES256' or key.get('ext', True) is not True
            or key.get('key_ops', ['sign']) != ['sign']):
        raise ManagerError('Keeper passkey key parameters are unsupported.')
    components = tuple(key.get(name) for name in ('d', 'x', 'y'))
    private, x, y = (decode_id(item, size=32) for item in components)
    try:
        material = ec.derive_private_key(int.from_bytes(private), ec.SECP256R1())
    except ValueError:
        raise ManagerError('Keeper passkey private key is invalid.') from None
    public = material.public_key().public_numbers()
    if public.x != int.from_bytes(x) or public.y != int.from_bytes(y):
        raise ManagerError('Keeper passkey public and private key components disagree.')
    return PasskeyCredential(relying_party_id, username, value['credentialId'], value['userId'], count,
                             material)


def retrieve_login(source, cli, expected_username, relying_party_id, run_private):
    """Resolve one exact selected record, then privately retrieve its fields."""
    from api_agent.auth.login_credentials import SelectedLoginCredentials, decode_id, validate_login_request
    validate_login_request(expected_username, relying_party_id)
    rows = _json_rows(run_private([cli, 'ls', '-l', '--format', 'json', f'{source.vault}/{source.user}']))
    if any(row.get('type') != 'record' or not isinstance(row.get('name'), str)
           or not isinstance(row.get('uid'), str) or not row['uid'] for row in rows):
        raise ManagerError('Keeper selected-login listing is not an unambiguous record listing.')
    selected = [row for row in rows if row['name'] == source.user]
    if not selected:
        return SelectedLoginCredentials(available=False)
    if len(selected) != 1:
        raise ManagerError('Keeper selected-login record is ambiguous.')
    uid = selected[0].get('uid')
    decode_id(uid, size=16)
    fields = _json_rows(run_private([cli, 'get', uid, '--format', 'fields', '--unmask']))
    if any(not isinstance(row.get('name'), str) for row in fields):
        raise ManagerError('Keeper selected-login fields are invalid.')
    if _single(fields, 'record_uid') != uid or _single(fields, 'title') != source.user:
        raise ManagerError('Keeper selected-login record does not match its selection.')
    login, password = _single(fields, 'login'), _single(fields, 'password')
    if login is not None and login != expected_username:
        raise ManagerError('Keeper selected-login username does not match.')
    if password is not None and not isinstance(password, str):
        raise ManagerError('Keeper selected-login password field is invalid.')
    value = _single(fields, 'passkey')
    passkey = None if value is None else _passkey(value, expected_username, relying_party_id)
    if login is None and passkey is None:
        raise ManagerError('Keeper selected-login username is unavailable.')
    return SelectedLoginCredentials(True, expected_username, password, passkey, (uid, source.user))
