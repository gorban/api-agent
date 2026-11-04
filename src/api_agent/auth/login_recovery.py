"""Optional login-only recovery shared by ordinary and selected credential reads."""


def recover_login(adapter, source, cli, interaction, timeout):
    """Return False without a probe; never grant output to a record command."""
    from .credentials import CredentialError, CredentialRecoveryError
    probe_hook = getattr(adapter, 'login_probe', None)
    if probe_hook is None:
        return False
    try:
        probe = probe_hook(source, cli)
    except CredentialError:
        raise
    except Exception:
        raise CredentialRecoveryError('Password manager safe login probe failed.') from None
    if (type(probe) is not list or not probe or probe[0] != cli
            or any(type(value) is not str or '\0' in value for value in probe)):
        raise CredentialError('Password manager returned an invalid credential command.')
    if probe != [cli, 'whoami']:
        raise CredentialRecoveryError('Password manager returned an unsafe login probe.')
    from .recovery import recover
    recover(probe, interaction, source._registry.get(source.manager).label, timeout,
            login_only=True)
    return True
