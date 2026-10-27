"""Isolated native AWS sign-in worker; stdout is a private bounded protocol."""
import datetime
import json
import math
import time
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Direct script launches inherit neither the launcher's sys.path nor its package
# context. Anchor imports to this worker's own source or installed package root.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from api_agent.llm_providers.bedrock.persistence import ProfileStore, atomic, capture_paths, fingerprint, KEYS, parsed, read

MAX_FRAME = 65536
MAX_OUTPUT = 262144



class CLIExecutionFailure(RuntimeError):
    """A native command failed; retain only an allowlisted classification."""
    def __init__(self, kind='unknown', reason='unknown'):
        if (kind, reason) not in _CLI_FAILURE_PAIRS:
            kind, reason = 'unknown', 'unknown'
        self.kind, self.reason = kind, reason
        super().__init__('Native command failed')


class CLIProtocolFailure(ValueError):
    """A fixed local pipe failure with no input or output retained."""
    def __init__(self, reason):
        self.reason = reason if reason in {
            'input_closed', 'input_limit', 'input_malformed',
            'input_multiline', 'output_limit',
        } else 'input_malformed'
        super().__init__('Native command protocol failed')


# Public CLI diagnostic contracts; dynamic values are matched and discarded.
_CLI_FAILURE_SIGNATURES = (
    ('invalid_request', 'invalid_code', r'Failed to decode the verification code\.'),
    ('invalid_request', 'invalid_code', r'Failed to retrieve an auth_code or state from the verification code'),
    ('authentication', 'expired_code', r"Error loading or redeeming a login authorization code: Unable to complete the login process due to an expired authorization code\. Please reauthenticate using 'aws login'\. *"),
    ('unavailable', 'tls', r'SSL validation failed for [^\r\n]+ [^\r\n]+'),
    ('unavailable', 'connectivity', r'Could not connect to the endpoint URL: "[^"\r\n]+"'),
    ('unavailable', 'connectivity', r'Connection was closed before we received a valid response from endpoint URL: "[^"\r\n]+"\.'),
    ('unavailable', 'connectivity', r'(?:Read|Connect) timeout on endpoint URL: "[^"\r\n]+"'),
    ('unavailable', 'connectivity', r'Failed to connect to proxy URL: "[^"\r\n]+"'),
    ('authorization', 'permissions', r"Unable to create or refresh login credentials due to insufficient permissions\. You may be missing permission for the 'signin:CreateOAuth2Token' action\."),
    ('authorization', 'permissions', r'An error occurred \((?:AccessDenied|AccessDeniedException|UnauthorizedException)\)(?: when calling the CreateOAuth2Token operation(?: \(reached max retries: [0-9]+\))?)?:[^\r\n]*'),
)
_CLI_FAILURE_PAIRS = frozenset((kind, reason) for kind, reason, _ in _CLI_FAILURE_SIGNATURES) | {('authentication', 'state_mismatch')}


def cli_failure(output):
    """Recognize complete diagnostic lines only; ambiguity remains unknown."""
    matches = set()
    for raw_line in output.decode('utf-8', errors='replace').splitlines():
        # Newer CLI writers add this exact prefix; older supported writers do not.
        line = raw_line.removeprefix('aws: [ERROR]: ')
        matches.update((kind, reason) for kind, reason, pattern in _CLI_FAILURE_SIGNATURES
                       if re.fullmatch(pattern, line))
        state_prefix = 'Error loading or redeeming a login authorization code: State parameter '
        state_line = line.rstrip(' ')
        if state_line.startswith(state_prefix) and state_line.endswith('.'):
            supplied, delimiter, expected = state_line[len(state_prefix):-1].partition(' does not match expected value ')
            if supplied and delimiter and expected:
                matches.add(('authentication', 'state_mismatch'))
    return CLIExecutionFailure(*next(iter(matches))) if len(matches) == 1 else CLIExecutionFailure()


class AcquisitionFailure(Exception):
    """Fixed protocol evidence; never retain or emit upstream diagnostic text."""
    def __init__(self, stage, kind, reason):
        self.stage, self.kind, self.reason = stage, kind, reason
        super().__init__(stage)


def diagnosed(stage, action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except (ImportError, AcquisitionFailure):
        raise
    except Exception as error:
        name = type(error).__name__
        response = getattr(error, 'response', None)
        detail = response.get('Error') if type(response) is dict else None
        code = detail.get('Code') if type(detail) is dict else None
        code = code if type(code) is str else None
        if isinstance(error, CLIProtocolFailure):
            kind, reason = 'invalid_request', error.reason
        elif isinstance(error, CLIExecutionFailure):
            kind, reason = error.kind, error.reason
        elif name == 'SSLError':
            kind, reason = 'unavailable', 'tls'
        elif name in {'EndpointConnectionError', 'ConnectionClosedError', 'ConnectTimeoutError', 'ReadTimeoutError'}:
            kind, reason = 'unavailable', 'connectivity'
        elif code in {'AccessDenied', 'AccessDeniedException', 'UnauthorizedException'}:
            kind, reason = 'authorization', 'rejected'
        elif code in {'ExpiredToken', 'ExpiredTokenException', 'InvalidClientTokenId', 'UnrecognizedClientException', 'InvalidToken'}:
            kind, reason = 'authentication', 'rejected'
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            kind, reason = 'invalid_request', 'malformed'
        else:
            kind, reason = 'unknown', 'unknown'
        raise AcquisitionFailure(stage, kind, reason) from None

def emit(value):
    raw = json.dumps(value).encode() + b'\n'
    if len(raw) > MAX_FRAME:
        raise ValueError
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def read_frame(stream):
    raw = stream.readline(MAX_FRAME + 1)
    if not raw.endswith(b'\n') or len(raw) > MAX_FRAME:
        raise ValueError
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError
    return value


def cli(command, environment, *, interactive=False, accept_input=True):
    """Drain bounded pipes; replies are private JSON, never terminal stdin."""
    output = bytearray()
    pending = bytearray()
    process = subprocess.Popen(command, stdin=subprocess.PIPE if interactive and accept_input else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            if interactive and accept_input:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'reply')
            while True:
                for key, _ in selector.select():
                    if key.data == 'reply':
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            raise CLIProtocolFailure('input_closed')
                        pending.extend(chunk)
                        if len(pending) > MAX_FRAME:
                            raise CLIProtocolFailure('input_limit')
                        while b'\n' in pending:
                            raw, _, remaining = pending.partition(b'\n')
                            pending[:] = remaining
                            try:
                                reply = json.loads(raw)
                            except (ValueError, UnicodeError):
                                raise CLIProtocolFailure('input_malformed') from None
                            if type(reply) is not dict or set(reply) != {'input'} or type(reply['input']) is not str:
                                raise CLIProtocolFailure('input_malformed')
                            if '\n' in reply['input'] or '\r' in reply['input']:
                                raise CLIProtocolFailure('input_multiline')
                            try:
                                native_input = reply['input'].encode() + b'\n'
                            except UnicodeError:
                                raise CLIProtocolFailure('input_malformed') from None
                            process.stdin.write(native_input)
                            process.stdin.flush()
                    else:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            if process.wait():
                                raise cli_failure(output)
                            return bytes(output)
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            raise CLIProtocolFailure('output_limit')
                        if interactive:
                            emit({'auth': chunk.decode('utf-8', errors='replace')})
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdin is not None:
            process.stdin.close()
        process.stdout.close()


def credentials(raw):
    value = json.loads(raw)
    if type(value) is not dict or type(value.get('Version')) is not int or value['Version'] != 1:
        raise ValueError
    for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken'):
        if type(value.get(key)) is not str or not value[key].strip():
            raise ValueError
    expiry = datetime.datetime.fromisoformat(value['Expiration'].replace('Z', '+00:00'))
    if expiry.tzinfo is None or expiry.timestamp() <= datetime.datetime.now(datetime.timezone.utc).timestamp():
        raise ValueError
    return {key: value[key] for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')}, expiry.timestamp()


def verify_identity(material, config, *, client_factory=None):
    import boto3
    from botocore.config import Config
    client = (boto3.client if client_factory is None else client_factory)('sts', region_name=config['region'],
                          aws_access_key_id=material['AccessKeyId'],
                          aws_secret_access_key=material['SecretAccessKey'],
                          aws_session_token=material.get('SessionToken'),
                          config=Config(connect_timeout=10, read_timeout=10, retries={'max_attempts': 0},
                                        ignore_configured_endpoint_urls=True, proxies={}))
    try:
        identity = client.get_caller_identity()
    finally:
        client.close()
    if not re.fullmatch(r'\d{12}', identity.get('Account', '')) or not isinstance(identity.get('Arn'), str) or not identity['Arn'].startswith('arn:'):
        raise ValueError
    principal = config.get('principal')
    arn = identity.get('Arn', '')
    if principal:
        parts = principal.split(':', 5)
        assumed_role = len(parts) == 6 and parts[2] == 'sts' and parts[5].startswith('assumed-role/')
        matches = arn.rpartition('/')[0] == principal if assumed_role else arn == principal
        if not matches:
            raise ValueError
    if config.get('account') and identity.get('Account') != config['account']:
        raise ValueError
    return {key: identity[key] for key in ('Account', 'Arn')}


def ambient(config):
    """Reuse a verified owned snapshot until its native refresh deadline."""
    # Resolve implicit profile names without converting them into explicit
    # selection, which would suppress the SDK's environment provider.
    profile = config.get('profile')
    implicit = profile is None
    if implicit:
        import boto3
        from botocore.exceptions import ProfileNotFound
        try:
            session = boto3.Session(profile_name=None, region_name=config.get('region'))
        except ProfileNotFound:
            return {'unavailable': True, 'sso': {}}
        profile = session.profile_name
    store = ProfileStore(capture_paths(), profile)
    # Readers and refreshers share publication's persistent lock. A peer must
    # observe one complete snapshot and recheck readiness after a prior refresh.
    # This worker remains owned by the existing acquisition deadline/cancellation.
    with store.locked():
        return _snapshot_ambient(config, store, profile, implicit,
                                 session if implicit else None)


def _snapshot_ambient(config, store, profile, implicit, session):
    if implicit:
        # An owned complete shared tuple can only be shadowed by earlier SDK
        # providers. Resolve the method before interpreting snapshot metadata.
        data = parsed(read(store.credentials))
        if data.has_section(profile) and all(data[profile].get(key) for key in KEYS):
            selected = session.get_credentials()
            if selected is None or selected.method != 'shared-credentials-file':
                return _ambient(config)
    marker = store.snapshot()
    if marker is None:
        return _ambient(config)
    identity = marker['identity']
    from api_agent.llm_providers.bedrock.acquisition import identity_binding
    account, principal = identity_binding(identity)
    # Explicit assertions still apply; the stored binding cannot loosen them.
    if config.get('account') and config['account'] != account or config.get('principal') and config['principal'] != principal:
        raise ValueError
    expiry = marker['expires_at']
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        raise ValueError('Invalid AWS snapshot expiry')
    if expiry > time.time() + 30:
        values = parsed(read(store.credentials))[store.profile]
        frozen = [values.get(key) for key in KEYS]
        if (fingerprint(frozen) != marker['fingerprint'] or
                any(type(value) is not str or not value.strip() for value in frozen)):
            raise ValueError('AWS snapshot changed')
        material = dict(zip(('AccessKeyId', 'SecretAccessKey', 'SessionToken'), frozen))
        region = config.get('region')
        if not region:
            if not implicit:
                import boto3
                # Region discovery does not resolve or refresh credentials.
                session = boto3.Session(profile_name=profile)
            region = session.region_name
        if not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region):
            raise ValueError('Invalid AWS snapshot region')
        try:
            verified = diagnosed('identity_verification', verify_identity, material,
                                 dict(config, region=region, account=account, principal=principal))
        except AcquisitionFailure as error:
            if (error.stage, error.kind, error.reason) != ('identity_verification', 'authentication', 'rejected'):
                raise
            # Local expiry is advisory: rejected owned material must use the
            # same masked native refresh path and retain its identity binding.
        else:
            return dict(material, expires_at=expiry, region=region, identity=verified)
    with tempfile.TemporaryDirectory(prefix='api-agent-snapshot-') as temporary:
        credentials = Path(temporary) / 'credentials'
        atomic(credentials, store.unmasked_credentials(), b'')
        previous = os.environ.get('AWS_SHARED_CREDENTIALS_FILE')
        os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(credentials)
        try:
            result = _ambient(dict(config, account=account, principal=principal))
        finally:
            if previous is None:
                os.environ.pop('AWS_SHARED_CREDENTIALS_FILE', None)
            else:
                os.environ['AWS_SHARED_CREDENTIALS_FILE'] = previous
    if 'unavailable' not in result:
        # SDK refresh may have renewed native caches. Export the verified tuple.
        material = {key: result[key] for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')}
        with tempfile.TemporaryDirectory(prefix='api-agent-persist-') as temporary:
            root = Path(temporary)
            (root / '.aws').mkdir(mode=0o700)
            store.seed(root)
            store.publish(root, material, result['expires_at'], result['identity'])
    else:
        result = dict(result, identity=identity)
    return result


def _ambient(config):
    """Resolve once, verify the exact tuple, and never let STS resolve a chain."""
    import boto3
    from botocore.exceptions import MissingDependencyException, ProfileNotFound
    try:
        session = boto3.Session(profile_name=config.get('profile'), region_name=config.get('region'))
    except ProfileNotFound:
        # Keep the requested profile; attended recovery creates only its owned
        # isolated copy. Never try another profile or a different SDK chain.
        return {'unavailable': True, 'sso': {}}
    # Read routing only; normal SDK refresh/helpers retain their own cache effects.
    full = session._session.full_config
    scoped = full.get('profiles', {}).get(session.profile_name, {})
    named = full.get('sso_sessions', {}).get(scoped.get('sso_session'), {})
    route = {key: scoped.get(key, named.get(key)) for key in
             ('sso_start_url', 'sso_region', 'sso_account_id', 'sso_role_name')}
    try:
        credentials = session.get_credentials()
        if credentials is None:
            return {'unavailable': True, 'sso': route}
        frozen = credentials.get_frozen_credentials()
        material = dict(AccessKeyId=frozen.access_key, SecretAccessKey=frozen.secret_key,
                        SessionToken=frozen.token)
        if not all(type(material[key]) is str and material[key].strip()
                   for key in ('AccessKeyId', 'SecretAccessKey')):
            raise ValueError
        region = config.get('region') or session.region_name
        if not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region):
            raise ValueError
        identity = diagnosed('identity_verification', verify_identity, material, dict(config, region=region), client_factory=session.client)
        expiry = getattr(credentials, '_expiry_time', None)
        expiry = None if expiry is None else expiry.timestamp()
        return dict(material, expires_at=expiry, region=region, identity=identity)
    except AcquisitionFailure as error:
        if (error.stage, error.kind, error.reason) == ('identity_verification', 'authentication', 'rejected'):
            return {'unavailable': True, 'sso': route}
        raise
    except Exception as error:
        if isinstance(error, MissingDependencyException):
            raise ImportError('AWS login SDK dependency unavailable') from None
        code = getattr(error, 'response', {}).get('Error', {}).get('Code')
        login = scoped.get('login_session')
        missing_login = (type(error).__name__ == 'LoginTokenLoadError' and type(login) is str and
            getattr(error, 'kwargs', {}).get('error_msg') ==
            'Unable to load a existing login session for session ' + login +
            ". Please reauthenticate with 'aws login'.")
        sso_name = scoped.get('sso_session') or route.get('sso_start_url')
        missing_sso = (type(error).__name__ == 'SSOTokenLoadError' and type(sso_name) is str and
            getattr(error, 'kwargs', {}).get('error_msg') == 'Token for ' + sso_name + ' does not exist')
        if (missing_login or missing_sso or type(error).__name__ in {'NoCredentialsError', 'PartialCredentialsError',
                'UnauthorizedSSOTokenError', 'LoginRefreshRequired'} or
                code in {'ExpiredToken', 'ExpiredTokenException', 'InvalidClientTokenId',
                         'UnrecognizedClientException', 'InvalidToken'}):
            return {'unavailable': True, 'sso': route}
        raise


def run(config):
    if config.get('method') == 'ambient':
        emit({'result': diagnosed('credential_resolution', ambient, config)})
        return
    store = ProfileStore(config['persistence'], config['profile']) if 'persistence' in config else None
    if store is not None and config['login']:
        diagnosed('credential_resolution', store.seed, os.environ['HOME'], configured=True)
    executable = shutil.which('aws')
    if executable is None:
        raise ImportError
    environment = dict(os.environ)
    version = cli([executable, '--version'], environment).decode()
    match = re.search(r'aws-cli/(\d+)\.(\d+)\.(\d+)', version)
    if not match or int(match[1]) != 2 or tuple(map(int, match.groups())) < (2, 32, 0):
        raise ImportError
    base = [executable, '--profile', config['profile'], '--region', config['region'],
            '--no-cli-pager', '--no-cli-auto-prompt']
    if config['login']:
        command = ['login', '--remote'] if config['method'] == 'direct' else ['sso', 'login', '--use-device-code', '--no-browser']
        diagnosed('sign_in', cli, base + command, environment, interactive=True, accept_input=config['method'] == 'direct')
        emit({'auth_done': True})
    raw = diagnosed('credential_export', cli, base + ['configure', 'export-credentials', '--format', 'process'], environment)
    material, expiry = diagnosed('result_validation', credentials, raw)
    identity = diagnosed('identity_verification', verify_identity, material, config)
    if store is not None:
        diagnosed('result_validation', store.publish, os.environ['HOME'], material, expiry, identity)
    emit({'result': dict(material, expires_at=expiry, identity=identity)})


def main():
    try:
        run(read_frame(getattr(sys.stdin.buffer, 'raw', sys.stdin.buffer)))
    except ImportError:
        emit({'error': 'setup'})
    except AcquisitionFailure as error:
        emit({'error': 'acquisition', 'stage': error.stage, 'kind': error.kind, 'reason': error.reason})
    except Exception:
        emit({'error': 'acquisition', 'stage': 'worker_protocol', 'kind': 'unknown', 'reason': 'unknown'})


if __name__ == '__main__':
    main()
