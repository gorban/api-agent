"""Declared GPT provider budgets, kept apart from the modules that spend them."""
import math

from api_agent.providers.contracts import ContractError

DEFAULT_DISCOVERY_TIMEOUT = 15


def discovery_timeout(config):
    """The configured model-catalog deadline in seconds; the declared default when unset."""
    value = config.get('GPT_DISCOVERY_TIMEOUT_SECONDS', DEFAULT_DISCOVERY_TIMEOUT)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ContractError('GPT_DISCOVERY_TIMEOUT_SECONDS must be a positive number of seconds.')
    return value
