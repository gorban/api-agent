"""Explicit Mantle model matrix qualified from the public AWS model cards.

https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7-flash.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-20b.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html

Runtime execution IDs and limits are qualified against the AWS model cards:
https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-openai.html
https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards-anthropic.html
https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html
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


# Runtime-qualified execution IDs from the corresponding AWS model cards.
# These entries supply optional metadata and native-schema qualification only.
_RUNTIME_OPENAI = {
    'openai.gpt-5.6-luna': (('us', 'in', 'global'), 'GPT 5.6 Luna', 1050000, 128000),
    'openai.gpt-5.6-terra': (('us', 'in', 'global'), 'GPT 5.6 Terra', 1050000, 128000),
    'openai.gpt-5.6-sol': (('us', 'global'), 'GPT 5.6 Sol', 1050000, 128000),
    'openai.gpt-6-astra': (('us', 'global'), 'GPT 6 Astra', 1050000, 128000),
    'openai.gpt-6-luna': (('us', 'global'), 'GPT 6 Luna', 1050000, 128000),
    'openai.gpt-6-sol': (('us', 'global'), 'GPT 6 Sol', 1050000, 128000),
    'openai.gpt-6.1-terra': (('us', 'global'), 'GPT 6.1 Terra', 1050000, 128000),
    'openai.gpt-6.1-sol': (('us', 'global'), 'GPT 6.1 Sol', 1000000, 131072),
}
_RUNTIME_ANTHROPIC = {
    'anthropic.claude-opus-4-8': (('us', 'eu', 'au', 'jp', 'us-gov', 'global'), 'Claude Opus 4.8', 1000000, 128000),
    'anthropic.claude-opus-4-7': (('us', 'eu', 'au', 'jp', 'global'), 'Claude Opus 4.7', 1000000, 128000),
    'anthropic.claude-opus-4-1-20250805-v1:0': (('us',), 'Claude Opus 4.1', 200000, 32000),
    'anthropic.claude-opus-4-5-20251101-v1:0': (('us', 'eu', 'global'), 'Claude Opus 4.5', 200000, 64000),
    'anthropic.claude-haiku-4-5-20251001-v1:0': (('us', 'eu', 'global'), 'Claude Haiku 4.5', 200000, 64000),
    'anthropic.claude-sonnet-4-5-20250929-v1:0': (('us', 'eu', 'au', 'jp', 'global'), 'Claude Sonnet 4.5', 200000, 64000),
    'anthropic.claude-sonnet-4-6': (('us', 'eu', 'au', 'jp', 'global'), 'Claude Sonnet 4.6', 1000000, 64000),
    'anthropic.claude-opus-4-6-v1': (('', 'us', 'eu', 'au', 'global'), 'Claude Opus 4.6', 1000000, 128000),
}
_RUNTIME = {}
for base, (prefixes, title, context, output) in _RUNTIME_OPENAI.items():
    for prefix in prefixes:
        _RUNTIME[prefix + '.' + base] = (title, 'openai', context, output)
for base, (prefixes, title, context, output) in _RUNTIME_ANTHROPIC.items():
    for prefix in prefixes:
        _RUNTIME[(prefix + '.' if prefix else '') + base] = (title, 'anthropic', context, output)
for suffix in ('20b', '120b'):
    _RUNTIME[f'openai.gpt-oss-{suffix}-1:0'] = (f'GPT OSS {suffix.upper()}', 'oss', 128000, 16000)


def selection(identifier, api_mode='completions', endpoint='mantle'):
    if endpoint == 'runtime':
        if (type(identifier) is not str or not 1 <= len(identifier) <= 256
                or not identifier.isprintable() or identifier != identifier.strip()
                or api_mode not in ('responses', 'completions', 'converse')):
            raise ContractError('Invalid Bedrock model or inference route.')
        metadata = _RUNTIME.get(identifier)
        features = BASE_FEATURES
        efforts = None if api_mode == 'converse' else ('low', 'medium', 'high')
        if efforts:
            features += ('reasoning_effort',)
        if metadata is None:
            return AvailableSelection(identifier, description='Runtime discovery does not establish API or feature availability.',
                                      features=features, reasoning_efforts=efforts, display_name=identifier,
                                      family=identifier)
        title, kind, context, output = metadata
        if ((kind == 'openai' and api_mode in ('responses', 'completions')
                or kind == 'anthropic' and api_mode == 'converse')
                and identifier != 'us.anthropic.claude-opus-4-1-20250805-v1:0'):
            features += ('structured',)
        parts = title.split()
        family = ('gpt-' + parts[-1].lower() if kind == 'openai' else
                  'claude-' + parts[1].lower() if kind == 'anthropic' else 'gpt-oss')
        version = parts[1] if kind == 'openai' else parts[-1] if kind == 'anthropic' else None
        prefix = identifier.split('.', 1)[0]
        scope = prefix if prefix in {'us', 'us-gov', 'eu', 'au', 'jp', 'in', 'global'} else 'direct'
        family += '-' + scope
        title += ' [' + scope.upper() + ']'
        return AvailableSelection(identifier, description='Runtime discovery does not establish API or feature availability.',
                                  features=features, reasoning_efforts=efforts, display_name=title,
                                  family=family, version=version, context_length=context, max_output_tokens=output)
    if (endpoint != 'mantle' or api_mode not in ('completions', 'responses') or type(identifier) is not str
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


def route(identifier, api_mode, endpoint='mantle'):
    selection(identifier, api_mode, endpoint)
    if endpoint == 'runtime':
        return '/converse' if api_mode == 'converse' else '/openai/v1'
    return '/v1'


def catalog(response, api_mode='completions', endpoint='mantle'):
    if endpoint not in ('mantle', 'runtime') or api_mode not in ('completions', 'responses', 'converse') or endpoint == 'mantle' and api_mode == 'converse':
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
        try:
            selected = selection(identifier, api_mode, endpoint)
        except ContractError:
            continue
        found.append(selected)
    return tuple(found)
