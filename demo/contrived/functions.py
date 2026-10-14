"""Portable, fixed bundled catalog; layout declarations cannot import code."""
from api_agent.workflows import contrived


async def produce(inputs, context):
    return await contrived.produce(inputs, context)


def validate_format(inputs, context):
    return contrived.validate_format(inputs, context)


def guard(inputs, context):
    return contrived.guard(inputs, context)
