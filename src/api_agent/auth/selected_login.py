"""Owned optional login retrieval; private reads never run in a recovery PTY."""
import math
import time

from api_agent.providers.progress import report_timeout_sync
from .login_credentials import SelectedLoginCredentials
from .managers import ManagerError


def retrieve(source, request, timeout, *, interaction=None, recovery_timeout=None):
    from . import credentials as c
    if not math.isfinite(timeout) or timeout <= 0:
        raise c.CredentialError('Credential CLI timeout must be positive.')
    source = (c.CredentialSource.from_mapping(vars(source), registry=source._registry)
              if isinstance(source, c.CredentialSource) else c.CredentialSource.from_mapping(source))
    try:
        adapter = source._registry.load(source.manager)
    except ManagerError:
        raise c.CredentialError('Password manager implementation is unavailable.') from None
    retrieve_login = getattr(adapter, 'retrieve_login', None)
    if retrieve_login is None:
        raise c.CredentialCapabilityUnsupportedError('Selected password manager does not support login credential discovery.')
    cli = c.resolve_cli_path(source.cli_path)
    def command(argv):
        if (type(argv) is not list or not argv or argv[0] != cli
                or any(type(value) is not str or '\0' in value for value in argv)):
            raise c.CredentialError('Password manager returned an invalid credential command.')
        return argv
    hook = getattr(adapter, 'classify_login', None)
    def classify(stderr):
        if hook is None:
            return False
        try:
            result = hook(stderr)
        except Exception:
            raise c.CredentialError('Password manager login classification failed.') from None
        if type(result) is not bool:
            raise c.CredentialError('Password manager returned an invalid login classification.')
        return result
    started, spent, recovered = time.monotonic(), 0.0, False
    def private_read(argv):
        nonlocal spent, recovered
        argv = command(argv)
        def run():
            nonlocal spent
            remaining = timeout - spent
            if remaining <= 0:
                raise c.CredentialError('Selected login retrieval deadline expired.')
            before = time.monotonic()
            try:
                with report_timeout_sync('Password-manager selected login', remaining) as budget:
                    if budget.remaining <= 0:
                        raise c.CredentialRecoveryError('Password manager recovery deadline expired.')
                    output = c._run(argv, budget.remaining, classify_login=classify,
                                    cancel_event=getattr(interaction, 'cancel_event', None))
                if type(output) is not str:
                    raise c.CredentialError('Credential CLI returned invalid output.')
                return output
            finally:
                spent += time.monotonic() - before
        try:
            return run()
        except c.CredentialLoginRequiredError:
            if interaction is None or recovered:
                raise
        recovered = True
        try:
            probe = command(adapter.login_probe(source, cli))
        except c.CredentialError:
            raise
        except Exception:
            raise c.CredentialRecoveryError('Password manager safe login probe failed.') from None
        # This optional capability only recovers authentication through a safe
        # session probe. Unmasked reads are always retried via owned capture.
        if probe != [cli, 'whoami']:
            raise c.CredentialRecoveryError('Password manager returned an unsafe login probe.')
        remaining = None if recovery_timeout is None else recovery_timeout - (time.monotonic() - started)
        with report_timeout_sync('Password-manager recovery', None) as budget:
            if budget.remaining is not None:
                remaining = budget.remaining if remaining is None else min(remaining, budget.remaining)
            from .recovery import recover
            recover(probe, interaction, source._registry.get(source.manager).label, remaining)
        return run()
    try:
        result = retrieve_login(source, cli, request.expected_username, request.relying_party_id, private_read)
    except c.CredentialError:
        raise
    except Exception:
        raise c.CredentialError('Selected login credential validation failed.') from None
    if type(result) is not SelectedLoginCredentials:
        raise c.CredentialError('Password manager returned invalid selected login credentials.')
    if result.available and (result.username != request.expected_username or
            result.passkey is not None and (result.passkey.username != request.expected_username
                                           or result.passkey.relying_party_id != request.relying_party_id)):
        raise c.CredentialError('Selected login credentials do not match the requested account and relying party.')
    return result
