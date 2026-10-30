"""Bounded HTTP/network retry decisions for a single logical inference."""
import math
import random
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

# Larger server minima are declined rather than shortened or awaited indefinitely.
MAX_RETRY_AFTER = 86400.0
UNSUPPORTED_RETRY_AFTER = MAX_RETRY_AFTER + 1


def retry_after(value, *, now=None):
    """Parse a bounded Retry-After header into a nonnegative finite duration."""
    if type(value) is not str:
        return None
    if len(value) > 128:
        return UNSUPPORTED_RETRY_AFTER
    value = value.strip()
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            return None
        return float(min(number, Decimal(str(UNSUPPORTED_RETRY_AFTER))))
    except InvalidOperation:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = (date - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, UNSUPPORTED_RETRY_AFTER)


def retryable(error):
    status = getattr(error, 'http_status', None)
    if type(status) is int and (status == 429 or 500 <= status <= 599):
        return True
    from .control import OperationalError
    return (status is None or type(status) is int and status == 200) and isinstance(error, OperationalError) and error.kind == 'network'


def transient_network_failure(error, transport):
    """Classify typed transport causes, not exception messages or broad OSErrors.

    Providers pass their actual httpx-compatible module (including SDK httpx2).
    Pool saturation, local deadlines, invalid URLs and certificate failures
    are deliberately excluded. A disconnected request may already be billable.
    """
    import ssl
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 16:
        seen.add(id(error))
        chain.append(error)
        error = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__)
    if any(isinstance(item, ssl.SSLError) and not isinstance(item, ssl.SSLEOFError) for item in chain):
        return False
    failures = (transport.ConnectError, transport.ConnectTimeout, transport.ReadError,
                transport.ReadTimeout, transport.WriteError, transport.WriteTimeout,
                transport.RemoteProtocolError)
    for item in chain:
        if isinstance(item, failures):
            return True
        if isinstance(item, (TimeoutError, transport.PoolTimeout, transport.InvalidURL,
                             transport.UnsupportedProtocol, transport.LocalProtocolError)):
            return False
    return False


def retry_hint_supported(hint):
    """Reject oversized server minima; malformed hints carry no minimum."""
    if type(hint) is int:
        return hint <= MAX_RETRY_AFTER
    return not (type(hint) is float and math.isfinite(hint) and hint > MAX_RETRY_AFTER)


def retry_delay(number, hint=None):
    nominal = min(30.0, 0.2 * 5 ** (number - 1))
    jittered = min(30.0, random.uniform(nominal * 0.8, nominal * 1.2))
    if (type(hint) not in (int, float) or hint < 0 or hint > MAX_RETRY_AFTER
            or (type(hint) is float and not math.isfinite(hint))):
        hint = 0
    return max(jittered, hint)
