"""Explicit interpretation of common text envelopes, independent of model ID.

Native fields are preferred. Text markers alone cannot distinguish a literal
answer from a reasoning envelope; auto mode refuses that ambiguous shape.
"""
from api_agent.providers.contracts import ProviderAccessError

TAGS = ('reasoning', 'think')


def may_be_envelope(text):
    return any(('<' + tag + '>').startswith(text) or text.startswith('<' + tag + '>') for tag in TAGS)


def final_answer(text, policy='auto', *, incomplete=False):
    if policy not in ('auto', 'tagged', 'literal'):
        raise ProviderAccessError('invalid_request', 'Invalid Bedrock reasoning format.')
    if policy == 'literal':
        return text
    tag = next((tag for tag in TAGS if text.startswith('<' + tag + '>')), None)
    if tag is None:
        return text
    if policy == 'auto':
        raise ProviderAccessError('invalid_request',
            'Ambiguous tagged Bedrock answer. Set BEDROCK_COMPLETIONS_REASONING_FORMAT=tagged '
            'only if this upstream uses reasoning envelopes, or literal to preserve its text.',
            code='reasoning_format_ambiguous')
    _, separator, answer = text.partition('</' + tag + '>')
    if not separator:
        if incomplete:
            return ''
        raise ProviderAccessError('unknown', 'Incomplete Bedrock reasoning envelope.',
                                  code='invalid_response')
    return answer


def display_reasoning(text, policy='auto', *, incomplete=False):
    """Read only an explicitly admitted leading envelope, never literal markup."""
    if policy != 'tagged':
        return ''
    tag = next((tag for tag in TAGS if text.startswith('<' + tag + '>')), None)
    if tag is None:
        return ''
    body = text[len(tag) + 2:]
    reasoning, separator, _ = body.partition('</' + tag + '>')
    if not separator:
        return '' if incomplete else reasoning
    return reasoning
