"""Bounded text inference; caller owns HTTP transport and selected authentication."""
import json

import httpx

from api_agent.providers.llm import LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.progress import component_timeout
from .discovery import _endpoint, _failure, _authentication_type
from .cache_policy import prompt_cache_fields

_LIMIT = 1048576
_TIMEOUT = 60


def _invalid(message='GPT inference returned an invalid result.'):
    return ProviderAccessError('unknown', message)


def _rejection(status, data, request_id, token, authentication_type=None, *, responses=True):
    reported = data.get('usage') if type(data) is dict else None
    if type(data) is dict and type(data.get('detail')) is str and 'error' not in data:
        data = {'error': {'message': data['detail']}}
    error = _failure(status, data, request_id, token, authentication_type)
    if error.message == 'GPT model discovery failed.':
        error.message = 'GPT inference was rejected.'
        error.args = (error.message,)
    error.execution = 'possibly_executed'
    return _reported_usage(error, reported, responses)


def _size(text):
    try:
        return len(text.encode('utf-8'))
    except UnicodeEncodeError:
        raise ProviderAccessError('invalid_request', 'GPT text contains invalid Unicode.') from None


def _payload(request, api_mode, subscription):
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'GPT inference requires an LLM request.')
    payload = request.payload
    if (payload.tools or payload.structured is not None or payload.generation.temperature is not None
            or any(row.calls or row.results or row.role == 'tool' for row in payload.messages)):
        raise ProviderAccessError('unsupported', 'GPT text inference does not support the requested feature.')
    identifier = request.selected_llm
    if (len(identifier) > 200 or not identifier.isprintable() or identifier.strip() != identifier
            or not identifier.startswith(('gpt-', 'openai/gpt-'))):
        raise ProviderAccessError('unsupported', 'GPT text inference requires an explicit GPT selection.')
    maximum = payload.generation.max_output_tokens
    if subscription and maximum is not None:
        raise ProviderAccessError('unsupported', 'GPT subscription inference does not support max_output_tokens.')
    if maximum is not None and maximum > 4096:
        raise ProviderAccessError('invalid_request', 'GPT text inference requires max_output_tokens between 1 and 4096.')
    rows = [{'role': 'developer' if subscription and row.role == 'system' else row.role, 'content': ''.join(part.text for part in row.content)} for row in payload.messages]
    if len(rows) > 64 or sum(_size(row['content']) for row in rows) > 65536:
        raise ProviderAccessError('invalid_request', 'GPT text inference request exceeded its limit.')
    body = {'model': identifier}
    if api_mode == 'responses':
        body.update(input=rows, store=False, stream=subscription)
        if not subscription:
            body['max_output_tokens'] = maximum or 128
    else:
        body.update(messages=rows, max_completion_tokens=maximum or 128, stream=False)
    return body


def _usage(data, responses):
    if data is None:
        return Usage()
    if type(data) is not dict:
        raise _invalid()
    input_name, output_name = ('input_tokens', 'output_tokens') if responses else ('prompt_tokens', 'completion_tokens')
    input_details = data.get('input_tokens_details' if responses else 'prompt_tokens_details')
    output_details = data.get('output_tokens_details' if responses else 'completion_tokens_details')
    if any(row is not None and type(row) is not dict for row in (input_details, output_details)):
        raise _invalid()
    try:
        usage = Usage(data.get(input_name), data.get(output_name), (input_details or {}).get('cached_tokens'),
                      (output_details or {}).get('reasoning_tokens'))
        if any(value is not None and value > 2147483647 for value in (usage.input_tokens,
                usage.output_tokens, usage.cached_input_tokens, usage.reasoning_tokens)):
            raise ValueError('Usage exceeded its limit')
        return usage
    except ValueError:
        raise _invalid() from None


def _reported_usage(error, data, responses):
    if data is not None:
        try:
            error.usage = _usage(data, responses)
        except ProviderAccessError:
            pass
    return error


def _result(data, responses, token):
    try:
        return _result_body(data, responses, token)
    except ProviderAccessError as error:
        _reported_usage(error, data.get('usage') if type(data) is dict else None, responses)
        raise


def _result_body(data, responses, token):
    if type(data) is not dict:
        raise _invalid()
    text, outcome = [], 'completed'
    if responses:
        if data.get('status') != 'completed' or type(data.get('output')) is not list:
            raise _invalid('GPT inference did not complete successfully.')
        for row in data['output']:
            if type(row) is not dict:
                raise _invalid()
            if row.get('type') == 'reasoning':
                continue
            if row.get('type') != 'message' or row.get('role') != 'assistant' or type(row.get('content')) is not list:
                raise _invalid()
            for part in row['content']:
                if type(part) is not dict or part.get('type') != 'output_text' or type(part.get('text')) is not str:
                    raise _invalid()
                text.append(part['text'])
    else:
        choices = data.get('choices')
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise _invalid()
        row = choices[0]
        message = row.get('message')
        if (row.get('finish_reason') not in ('stop', 'length') or type(message) is not dict
                or message.get('role') != 'assistant' or type(message.get('content')) is not str
                or message.get('tool_calls') or message.get('refusal')):
            raise _invalid()
        text.append(message['content'])
        outcome = 'length' if row['finish_reason'] == 'length' else 'completed'
    joined = ''.join(text)
    if token in joined or token in ''.join(character for character in joined if character.isprintable()):
        raise _invalid('GPT inference returned protected authentication content.')
    if not joined or _size(joined) > 65536:
        raise _invalid()
    model = data.get('model')
    if model is not None and (type(model) is not str or not 0 < len(model) <= 200 or not model.isprintable() or token in model):
        raise _invalid('GPT inference returned an invalid model identity.')
    tier, revision = data.get('service_tier'), data.get('serving_revision')
    if any(value is not None and (type(value) is not str or not 0 < len(value) <= 200 or not value.isprintable() or token in value) for value in (tier, revision)):
        raise _invalid('GPT inference returned invalid billing metadata.')
    return LLMResult(Message('assistant', (TextContent(joined),)), _usage(data.get('usage'), responses), outcome, model, tier, revision)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Invalid JSON constant')


def _json(body):
    try:
        return json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _invalid() from None


def _stream_identity(item, *, finished=False):
    if type(item) is not dict:
        raise _invalid()
    identifier = item.get('id')
    kind = item.get('type')
    if (type(identifier) is not str or not identifier or len(identifier) > 200
            or not identifier.isprintable() or kind not in ('message', 'reasoning')):
        raise _invalid()
    if finished and (item.get('status') != 'completed' and not (kind == 'reasoning' and item.get('status') is None)):
        raise _invalid('GPT inference returned an unfinished output item.')
    return identifier, kind


def _stream_completed(response, added, completed, token):
    if type(response) is not dict or type(response.get('output')) is not list:
        raise _invalid()
    if added:
        if set(added) != set(completed) or set(completed) != set(range(len(completed))):
            raise _invalid('GPT inference stream contains unfinished or missing output items.')
        recovered = [completed[index] for index in range(len(completed))]
        terminal = response['output']
        if terminal:
            if len(terminal) != len(recovered):
                raise _invalid('GPT inference terminal output conflicts with finished items.')
            for left, right in zip(terminal, recovered):
                if (_stream_identity(left, finished=True) != _stream_identity(right, finished=True)
                        or left.get('role') != right.get('role') or left.get('content') != right.get('content')):
                    raise _invalid('GPT inference terminal output conflicts with finished items.')
        response = dict(response, output=recovered)
    return _result(response, True, token)


def _sse(body, token, request_id):
    try:
        text = body.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        if not text.endswith('\n\n'):
            raise _invalid('GPT inference stream ended before completion.')
    except UnicodeDecodeError:
        raise _invalid() from None
    final = None
    added, completed, identifiers = {}, {}, set()
    response_id = None
    for block in text.split('\n\n'):
        values = [line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:')]
        if not values:
            continue
        try:
            data = _json('\n'.join(values))
        except ProviderAccessError as error:
            if final is not None:
                error.usage = final.usage
            raise
        if type(data) is not dict or type(data.get('type')) is not str or final is not None:
            error = _invalid()
            if final is not None:
                error.usage = final.usage
            raise error
        kind = data['type']
        if kind in ('response.created', 'response.in_progress'):
            response = data.get('response')
            if type(response) is dict and 'id' in response:
                identifier = response['id']
                if (type(identifier) is not str or not identifier or len(identifier) > 200
                        or not identifier.isprintable() or (response_id is not None and response_id != identifier)):
                    raise _invalid()
                response_id = identifier
        if 'output_index' in data:
            index = data['output_index']
            if type(index) is not int or not 0 <= index < 64:
                raise _invalid()
            if kind == 'response.output_item.added':
                identity = _stream_identity(data.get('item'))
                if index in added or identity[0] in identifiers:
                    raise _invalid('GPT inference stream contains duplicate output items.')
                added[index] = identity
                identifiers.add(identity[0])
            elif kind == 'response.output_item.done':
                item = data.get('item')
                if index not in added or index in completed or _stream_identity(item, finished=True) != added[index]:
                    raise _invalid('GPT inference stream contains inconsistent output items.')
                completed[index] = item
            else:
                if index not in added or index in completed or data.get('item_id') != added[index][0]:
                    raise _invalid('GPT inference stream contains inconsistent output identities.')
                if 'content_index' in data and (type(data['content_index']) is not int or not 0 <= data['content_index'] < 64):
                    raise _invalid()
        elif kind in ('response.output_item.added', 'response.output_item.done'):
            raise _invalid()
        if kind in ('response.failed', 'response.incomplete', 'error'):
            response = data.get('response')
            if kind == 'response.incomplete':
                raise _reported_usage(_invalid('GPT inference returned an incomplete response.'),
                                      response.get('usage') if type(response) is dict else None, True)
            failure = response if type(response) is dict else data
            if kind == 'error' and 'error' not in failure:
                failure = {'error': failure}
            raise _rejection(200, failure, request_id, token)
        if kind == 'response.completed':
            response = data.get('response')
            if response_id is not None and (type(response) is not dict or response.get('id') != response_id):
                raise _invalid('GPT inference terminal response identity changed.')
            try:
                final = _stream_completed(response, added, completed, token)
            except ProviderAccessError as error:
                _reported_usage(error, response.get('usage') if type(response) is dict else None, True)
                raise
    if final is None:
        raise _invalid('GPT inference stream ended before completion.')
    return final


def validate_text_request(*, base_url, access_token, subscription, api_mode, request):
    """Pure validation before the factory allocates its HTTP client."""
    if api_mode not in ('responses', 'completions') or (subscription and api_mode != 'responses'):
        raise ProviderAccessError('invalid_request', 'GPT subscription inference requires responses mode.')
    base_url = _endpoint(base_url, access_token, subscription)
    body = _payload(request, api_mode, subscription)
    body.update(prompt_cache_fields(request.payload.generation.cache_policy, request.selected_llm,
                                  base_url=base_url, subscription=subscription))
    return base_url, body


async def complete_text(*, client, base_url, access_token, subscription, api_mode, request):
    """One request, no discovery, retry, credential fallback or result cache."""
    base_url, body = validate_text_request(base_url=base_url, access_token=access_token,
        subscription=subscription, api_mode=api_mode, request=request)
    try:
        async with component_timeout('GPT text inference', _TIMEOUT) as budget:
            async with client.stream('POST', base_url + ('/responses' if api_mode == 'responses' else '/chat/completions'),
                    json=body, headers={'Authorization': 'Bearer ' + access_token, 'Accept-Encoding': 'identity'},
                    follow_redirects=False, timeout=budget.remaining) as response:
                if response.headers.get('content-encoding', '').strip().lower() not in ('', 'identity'):
                    raise _invalid('GPT inference returned unsupported content encoding.')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > _LIMIT:
                        raise _invalid('GPT inference response exceeded its limit.')
                    raw.extend(chunk)
                request_id = response.headers.get('x-request-id')
                if response.status_code != 200:
                    try:
                        data = _json(raw)
                    except ProviderAccessError:
                        data = None
                    raise _rejection(response.status_code, data, request_id, access_token,
                                   _authentication_type(response.headers.get('www-authenticate')), responses=api_mode == 'responses')
                if subscription:
                    if response.headers.get('content-type', '').split(';')[0].strip().lower() not in ('', 'text/event-stream'):
                        raise _invalid('GPT subscription inference requires an event stream.')
                    return _sse(raw, access_token, request_id)
                return _result(_json(raw), api_mode == 'responses', access_token)
    except (TimeoutError, httpx.TimeoutException):
        error = ProviderAccessError('deadline', 'GPT inference timed out.')
        error.execution = 'possibly_executed'
        raise error from None
    except httpx.RequestError:
        error = ProviderAccessError('unavailable', 'GPT inference transport is unavailable.')
        error.execution = 'possibly_executed'
        raise error from None
