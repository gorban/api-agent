"""Bounded combinations of assistant text and parsed tool argument values."""
from .contracts import ContractError


def observable_response_carriers(text, tool_arguments):
    """Yield text plus each string value in both orders, within an 8 MiB budget.

    Arguments are already parsed JSON values. Object keys and independent values
    are not combined: each yielded carrier pairs assistant text with one leaf.
    """
    def values(value):
        if type(value) is str:
            yield value
        elif type(value) is list:
            for child in value:
                yield from values(child)
        elif type(value) is dict:
            for child in value.values():
                yield from values(child)
    budget = 0
    for arguments in tool_arguments:
        for value in values(arguments):
            budget += 2 * (len(text) + len(value))
            if budget > 8 * 1024 * 1024:
                raise ContractError('Credential protection limit exceeded')
            yield text + value
            yield value + text
