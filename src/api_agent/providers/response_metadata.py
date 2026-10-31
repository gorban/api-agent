"""Bounded public Responses text-logprob metadata shared by provider codecs."""
import math

from .contracts import ContractError


def _logprob(row, *, top=False):
    keys = {'token', 'bytes', 'logprob'} | (set() if top else {'top_logprobs'})
    if type(row) is not dict or set(row) != keys:
        raise ContractError('Invalid native logprob metadata')
    if type(row['token']) is not str or len(row['token']) > 4096:
        raise ContractError('Invalid native logprob metadata')
    raw = row['bytes']
    if type(raw) is not list or len(raw) > 4096 or any(type(value) is not int or not 0 <= value <= 255 for value in raw):
        raise ContractError('Invalid native logprob metadata')
    value = row['logprob']
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ContractError('Invalid native logprob metadata')
    if not top:
        alternatives = row['top_logprobs']
        if type(alternatives) is not list or len(alternatives) > 20:
            raise ContractError('Invalid native logprob metadata')
        for alternative in alternatives:
            _logprob(alternative, top=True)


def _logprobs(value):
    if value is None:
        return
    if type(value) is not list or len(value) > 1024:
        raise ContractError('Invalid native logprob metadata')
    for row in value:
        _logprob(row)
