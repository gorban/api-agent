"""Bounded JSON representation of the documented Converse binary union.

Only reasoningContent.redactedContent is encoded; arbitrary binary SDK fields
remain invalid. Wire state is private replay data, never displayable reasoning.
"""
import base64

from api_agent.providers.contracts import ContractError

MAX_OPAQUE_BYTES = 65536


def binary(value):
    if type(value) is not str or not value or len(value) > 4 * ((MAX_OPAQUE_BYTES + 2) // 3):
        raise ContractError('Invalid Converse opaque state')
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ContractError('Invalid Converse opaque state') from None
    if (not decoded or len(decoded) > MAX_OPAQUE_BYTES
            or base64.b64encode(decoded).decode('ascii') != value):
        raise ContractError('Invalid Converse opaque state')
    return decoded


def guard_binary(value, secrets):
    from api_agent.telemetry.redaction import registered_byte_ranges
    from api_agent.providers.credential_guard import CredentialProtectionError
    if registered_byte_ranges(value, secrets):
        raise CredentialProtectionError('Content cannot be safely released.')


def wire_response(value, secrets):
    if type(value) is not dict or type(value.get('output')) is not dict:
        return value
    native = value['output'].get('message')
    if type(native) is not dict or type(native.get('content')) is not list:
        return value
    output = {**value, 'output': {**value['output'], 'message': {**native, 'content': []}}}
    blocks = output['output']['message']['content']
    total = 0
    for original in native['content']:
        block = dict(original) if type(original) is dict else original
        if type(block) is dict and type(block.get('reasoningContent')) is dict:
            block['reasoningContent'] = dict(block['reasoningContent'])
        blocks.append(block)
        reasoning = block.get('reasoningContent') if type(block) is dict else None
        if type(reasoning) is dict and 'redactedContent' in reasoning:
            raw = reasoning['redactedContent']
            if set(reasoning) != {'redactedContent'} or type(raw) not in (bytes, bytearray) or not raw:
                raise ContractError('Invalid Converse opaque state')
            total += len(raw)
            if total > MAX_OPAQUE_BYTES:
                raise ContractError('Converse opaque state exceeds limit')
            guard_binary(bytes(raw), secrets)
            reasoning['redactedContent'] = base64.b64encode(raw).decode('ascii')
    return output


def sdk_messages(messages, secrets):
    if type(messages) is not list:
        raise ContractError('Invalid Converse messages')
    output = []
    total = 0
    for original in messages:
        if type(original) is not dict or type(original.get('content', [])) is not list:
            raise ContractError('Invalid Converse message')
        message = {**original, 'content': []}
        output.append(message)
        for original_block in original.get('content', []):
            block = dict(original_block) if type(original_block) is dict else original_block
            if type(block) is dict and type(block.get('reasoningContent')) is dict:
                block['reasoningContent'] = dict(block['reasoningContent'])
            message['content'].append(block)
            reasoning = block.get('reasoningContent') if type(block) is dict else None
            if type(reasoning) is dict and 'redactedContent' in reasoning:
                if set(reasoning) != {'redactedContent'}:
                    raise ContractError('Invalid Converse opaque state')
                raw = binary(reasoning['redactedContent'])
                total += len(raw)
                if total > MAX_OPAQUE_BYTES:
                    raise ContractError('Converse opaque state exceeds limit')
                guard_binary(raw, secrets)
                reasoning['redactedContent'] = raw
    return output
