"""The shared JSON response instruction, also retained by workflow histories."""
from .llm import Message, TextContent


def json_instruction(schema):
    return Message('user', (TextContent(
        'Return a complete final answer containing only JSON satisfying this response schema. '
        'Include the final JSON answer even when reasoning is enabled; reasoning is not the answer.\n'
        + schema),))
