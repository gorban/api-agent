"""Bounded combinations of assistant text and parsed tool argument values."""
from .contracts import ContractError
from itertools import chain, combinations


def observable_response_carriers(text, tool_arguments, *, summary=None, additional_text=()):
    """Yield text plus each string value in both orders, within an 8 MiB budget.

    Arguments are already parsed JSON values. Object keys and independent values
    are not combined: each yielded carrier pairs assistant text with one leaf.
    A separately displayed summary is also paired with the answer and each tool
    leaf. Additional admitted plaintext carriers, including undisplayed native
    replay text, share these pairs and the same lazily enforced budget.
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
    texts = ((text, summary) if summary else (text,)) + tuple(additional_text)
    pairs = chain(combinations(texts, 2),
                  ((carrier, value) for arguments in tool_arguments
                   for value in values(arguments) for carrier in texts))
    for carrier, value in pairs:
        budget += 2 * (len(carrier) + len(value))
        if budget > 8 * 1024 * 1024:
            raise ContractError('Credential protection limit exceeded')
        yield carrier + value
        yield value + carrier
