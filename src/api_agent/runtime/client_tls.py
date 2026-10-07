"""Explicit public-CA trust for clients; never disable TLS verification."""
from pathlib import Path
import ssl


class ClientTLSError(ValueError):
    """Stable diagnostics do not expose CA paths or file contents."""


def require_verified_context(context):
    if (not isinstance(context, ssl.SSLContext)
            or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname):
        raise ClientTLSError('Client TLS must verify certificates and hostnames.')
    return context


def verified_client_context(ca_file=None):
    """Keep system roots and optionally add a local, public certificate bundle.

    The caller explicitly provides the public CA produced by HTTPS preparation.
    This helper neither generates certificates nor modifies host trust.
    """
    try:
        context = ssl.create_default_context()
        if ca_file is not None:
            path = Path(ca_file)
            if not path.is_file():
                raise ValueError
            public = path.read_text(encoding='ascii')
            if 'PRIVATE KEY' in public or '-----BEGIN CERTIFICATE-----' not in public:
                raise ValueError
            context.load_verify_locations(cadata=public)
        return require_verified_context(context)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ClientTLSError('Client public CA could not be loaded.') from None
