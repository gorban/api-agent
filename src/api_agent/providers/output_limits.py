"""Provider-owned output ceilings and explicit pre-inference negotiation."""
from dataclasses import replace
import math
import time

from .contracts import ContractError, ProviderAccessError


class OutputLimitRejected(ProviderAccessError):
    """A provider-confirmed output ceiling rejected before inference started.

    Adapters must establish this from their upstream contract. Text matching in
    shared inference code is deliberately insufficient to authorize a replay.
    """
    execution = 'not_started'

    def __init__(self, maximum_tokens, message='Requested output exceeds the provider maximum.', *, status=400, code=None, request_id=None):
        if type(maximum_tokens) is not int or not 1 <= maximum_tokens <= 1048576 or type(status) is not int or status not in (400, 422):
            raise ContractError('Invalid output limit rejection')
        super().__init__('invalid_request', message, status=status, code=code, request_id=request_id)
        self.maximum_tokens = maximum_tokens
        self.http_status = status


def with_output_limit(request, maximum):
    return replace(request, payload=replace(request.payload,
        generation=replace(request.payload.generation, max_output_tokens=maximum)))


def clamp_request(request, selection, maximum_tokens=200000, learned_maximum=None):
    """Apply advertised output support without changing the caller's request."""
    if selection is None or 'max_output_tokens' not in selection.features:
        return request
    requested = request.payload.generation.max_output_tokens
    bounds = (requested, maximum_tokens, selection.max_output_tokens, learned_maximum)
    return with_output_limit(request, min(value for value in bounds if value is not None))


class OutputLimitCache:
    """One selected provider/account/endpoint owner's bounded model cache."""
    def __init__(self, ttl=3600):
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl < 0:
            raise ContractError('Invalid output limit cache expiry')
        self.ttl = ttl
        self._entries = {}

    def get(self, model):
        entry = self._entries.get(model)
        if entry is None:
            return None
        maximum, expires = entry
        if time.monotonic() >= expires:
            del self._entries[model]
            return None
        return maximum

    def remember(self, model, maximum):
        previous = self.get(model)
        # Concurrent discoveries cannot replace a smaller live bound.
        if model not in self._entries and len(self._entries) >= 256:
            del self._entries[next(iter(self._entries))]
        self._entries[model] = (min(previous, maximum) if previous is not None else maximum,
                                time.monotonic() + self.ttl)


def rejected_limit(error, request):
    if (not isinstance(error, OutputLimitRejected) or error.origin != 'provider'
            or error.execution != 'not_started' or error.http_status not in (400, 422)
            or error.usage is not None):
        return None
    maximum = request.payload.generation.max_output_tokens
    reported = error.maximum_tokens
    return reported if type(reported) is int and maximum is not None and 0 < reported < maximum else None
