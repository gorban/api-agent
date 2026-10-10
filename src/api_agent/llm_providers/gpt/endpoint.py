"""Validate API-key backend bases before any credential is retrieved."""
import ipaddress
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from api_agent.providers.contracts import ContractError


OFFICIAL_BASE_URL = "https://api.openai.com/v1"


def validate_base_url(value) -> str:
    """Return an HTTPS base with lowercase origin and no trailing slash."""
    message = "GPT_BASE_URL must be an absolute HTTPS base without credentials, query, or fragment."
    try:
        if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError
        if any(c in value for c in ('\\', '?', '#')):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.netloc.endswith(':') or parsed.port == 0:
            raise ValueError
        if ':' in host:
            if '%' in host:
                raise ValueError
            ipaddress.IPv6Address(host)
        else:
            labels = host.split('.')
            if len(host) > 253 or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) for label in labels):
                raise ValueError
            if re.fullmatch(r'[0-9.]+', host):
                ipaddress.IPv4Address(host)
        path = parsed.path
        if re.search(r'%(?![0-9a-fA-F]{2})', path) or not re.fullmatch(r"[A-Za-z0-9/._~!$&'()*+,;=:@%\-]*", path):
            raise ValueError
        decoded = unquote(path, errors='strict')
        if any(ord(c) <= 32 or not c.isprintable() for c in decoded) or '\\' in decoded:
            raise ValueError
        if '//' in decoded or any(part in {'.', '..'} for part in decoded.split('/')):
            raise ValueError
        if re.search(r'%2f|%5c', path, re.IGNORECASE):
            raise ValueError
        return urlunsplit(('https', parsed.netloc.lower(), path.rstrip('/'), '', ''))
    except (ValueError, UnicodeError):
        raise ContractError(message) from None
