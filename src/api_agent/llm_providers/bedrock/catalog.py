"""Explicit Mantle model matrix qualified from the public AWS model cards.

https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7-flash.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-20b.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html

K-token limits use conservative decimal units as published by AWS.
"""
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import AvailableSelection

BASE_FEATURES = ('tools', 'temperature', 'max_output_tokens')
# Provider-level union; individual selections remain the request authority.
FEATURES = BASE_FEATURES + ('reasoning_effort', 'structured')
_METADATA = {
    'zai.glm-5': ('GLM 5', 'glm', '5', 200000, 128000),
    'zai.glm-4.7': ('GLM 4.7', 'glm', '4.7', 203000, 128000),
    'zai.glm-4.7-flash': ('GLM 4.7 Flash', 'glm-flash', '4.7', 203000, 128000),
    'openai.gpt-oss-20b': ('GPT OSS 20B', 'gpt-oss', None, 128000, 16000),
    'openai.gpt-oss-120b': ('GPT OSS 120B', 'gpt-oss', None, 128000, 16000),
}
MODELS = frozenset(_METADATA)


def selection(identifier, api_mode='completions'):
    if (api_mode not in ('completions', 'responses') or type(identifier) is not str
            or identifier not in MODELS or api_mode == 'responses' and not identifier.startswith('openai.')):
        raise ContractError('Unsupported Bedrock model or inference route.')
    title, family, version, context, output = _METADATA[identifier]
    description = 'AWS model-card token caps interpreted conservatively in decimal K units.'
    if identifier in ('zai.glm-4.7', 'zai.glm-4.7-flash'):
        description += ' AWS lists this model as legacy, with EOL March 30, 2027.'
    efforts = ('low', 'medium', 'high') if identifier.startswith('openai.') else None
    features = BASE_FEATURES + (('reasoning_effort',) if efforts else ())
    if identifier == 'zai.glm-5' and api_mode == 'completions':
        features += ('structured',)
    if efforts:
        description += ' Native categorical reasoning effort: low, medium, high; reasoning cannot be disabled.'
    return AvailableSelection(identifier, description=description, features=features, reasoning_efforts=efforts, display_name=title,
                              family=family, version=version, context_length=context,
                              max_output_tokens=output)


def route(identifier, api_mode):
    selection(identifier, api_mode)
    return '/v1'


def catalog(response, api_mode='completions'):
    if api_mode not in ('completions', 'responses'):
        raise ContractError('Unsupported Bedrock API mode.')
    if (type(response) is not dict or type(response.get('data')) is not list
            or len(response['data']) > 1024 or response.get('has_more', False) is not False
            or any(response.get(key) is not None for key in
                   ('next_page', 'next_token', 'next_cursor', 'next_page_token', 'nextPageToken'))):
        raise ContractError('Invalid or incomplete Bedrock catalog.')
    found, seen = [], set()
    for row in response['data']:
        if (type(row) is not dict or type(row.get('id')) is not str
                or not 1 <= len(row['id']) <= 256 or row['id'] in seen):
            raise ContractError('Invalid or duplicate Bedrock catalog entry.')
        identifier = row['id']
        seen.add(identifier)
        if identifier in MODELS and (api_mode == 'completions' or identifier.startswith('openai.')):
            found.append(selection(identifier, api_mode))
    return tuple(found)
