"""Bounded text inference; caller owns HTTP transport and selected authentication."""
from asyncio import CancelledError
from dataclasses import replace
import json
from uuid import UUID

import httpx

from api_agent.providers.llm import LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.progress import component_timeout
from .discovery import _endpoint, _failure, _authentication_type
from .cache_policy import prompt_cache_fields

_LIMIT = 1048576
_TIMEOUT = 120
_RETRY_CODE = "subscription_sharing_user_unavailable"


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


def _payload(request, api_mode, subscription, *, _validate_instances=True):
    if not isinstance(request, LLMRequest):
        raise ProviderAccessError('invalid_request', 'GPT inference requires an LLM request.')
    payload = request.payload
    if payload.tools or payload.structured is not None:
        from .rich import payload as rich_payload
        return rich_payload(request, api_mode, subscription, _validate_instances=_validate_instances)
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
    for message in payload.messages:
        for part in message.content:
            _size(part.text)
    from api_agent.providers.rich_inference import request_to_wire
    from api_agent.providers.contracts import ContractError
    try:
        request_to_wire(request, _validate_instances=_validate_instances)
    except ContractError:
        raise ProviderAccessError('invalid_request', 'Invalid GPT text inference request.') from None
    rows = [{'role': 'developer' if subscription and row.role == 'system' else row.role, 'content': ''.join(part.text for part in row.content)} for row in payload.messages]
    body = {'model': identifier}
    if api_mode == 'responses':
        body.update(input=rows, store=False, stream=subscription)
        if not subscription:
            body['max_output_tokens'] = maximum or 128
    else:
        body.update(messages=rows, max_completion_tokens=maximum or 128, stream=False)
    return body


def _usage(data, responses, *, router=False):
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
                      (output_details or {}).get('reasoning_tokens'), (input_details or {}).get('cache_write_tokens', 0 if router else None))
        if any(value is not None and value > 2147483647 for value in (usage.input_tokens,
                usage.output_tokens, usage.cached_input_tokens, usage.reasoning_tokens, usage.cache_write_tokens)):
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
    if (responses and isinstance(data.get('output'), list) and any(isinstance(item, dict) and item.get('type') == 'function_call' and 'call_id' in item for item in data['output'])) or (not responses and isinstance(data.get('choices'), list) and any(isinstance(item, dict) and item.get('finish_reason') == 'tool_calls' for item in data['choices'])):
        from .rich import tool_result
        return tool_result(data, responses, token)
    text, outcome = [], 'completed'
    if responses:
        if data.get('status') not in ('completed', 'incomplete') or type(data.get('output')) is not list:
            raise _invalid('GPT inference did not complete successfully.')
        if data['status'] == 'incomplete':
            if type(data.get('incomplete_details')) is not dict or data['incomplete_details'].get('reason') != 'max_output_tokens':
                raise _invalid('GPT inference did not complete successfully.')
            outcome = 'length'
        for row in data['output']:
            if type(row) is not dict:
                raise _invalid()
            if row.get('status') not in (None, 'completed') and not (outcome == 'length' and row.get('status') == 'incomplete'):
                raise _invalid('GPT inference returned an unfinished output item.')
            if row.get('type') == 'reasoning':
                continue
            if row.get('type') != 'message' or row.get('role') != 'assistant' or type(row.get('content')) is not list:
                raise _invalid()
            for part in row['content']:
                if type(part) is dict and part.get('type') == 'refusal' and type(part.get('refusal')) is str:
                    text.append(part['refusal'])
                    outcome = 'refused'
                    continue
                if type(part) is not dict or part.get('type') != 'output_text' or type(part.get('text')) is not str:
                    raise _invalid()
                text.append(part['text'])
    else:
        choices = data.get('choices')
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise _invalid()
        row = choices[0]
        message = row.get('message')
        if type(message) is dict and type(message.get('refusal')) is str and message['refusal'] and row.get('finish_reason') == 'stop' and not message.get('tool_calls') and message.get('role') == 'assistant':
            message = dict(message, content=message['refusal'], refusal=None)
            outcome = 'refused'
        if (row.get('finish_reason') not in ('stop', 'length') or type(message) is not dict
                or message.get('role') != 'assistant' or type(message.get('content')) is not str
                or message.get('tool_calls') or message.get('refusal')):
            raise _invalid()
        text.append(message['content'])
        outcome = 'length' if row['finish_reason'] == 'length' else outcome
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


def _billing(data, request_id, token):
    from api_agent.providers.costs import validate_billing
    billing = dict(usd=(data.get('usage') or {}).get('cost'),
                   request_id=data.get('request_id', request_id), generation_id=data.get('id'))
    try:
        validate_billing(billing)
        if any(token in value for value in (billing['request_id'], billing['generation_id']) if value is not None):
            raise ValueError()
    except ValueError:
        raise _invalid('GPT inference returned invalid billing metadata.') from None
    return billing


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
            or not identifier.isprintable() or kind not in ('message', 'reasoning', 'function_call')):
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
                        or any(left.get(key) != right.get(key) for key in ('role', 'content', 'phase', 'call_id', 'name', 'arguments', 'encrypted_content', 'summary'))):
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
    arguments, argument_done, call_identity = {}, set(), {}
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
                if identity[1] == 'function_call':
                    item = data['item']
                    if type(item.get('call_id')) is not str or type(item.get('name')) is not str or item.get('arguments') not in (None, ''):
                        raise _invalid('GPT inference stream contains invalid function identity.')
                    call_identity[index] = (item['call_id'], item['name'])
            elif kind == 'response.output_item.done':
                item = data.get('item')
                if index not in added or index in completed or _stream_identity(item, finished=True) != added[index]:
                    raise _invalid('GPT inference stream contains inconsistent output items.')
                if added[index][1] == 'function_call':
                    if (item.get('call_id'), item.get('name')) != call_identity[index]:
                        raise _invalid('GPT inference stream changed function identity.')
                    if index in arguments and item.get('arguments') != arguments[index]:
                        raise _invalid('GPT inference stream changed function arguments.')
                completed[index] = item
            else:
                if index not in added or index in completed or data.get('item_id') != added[index][0]:
                    raise _invalid('GPT inference stream contains inconsistent output identities.')
                if kind in ('response.function_call_arguments.delta', 'response.function_call_arguments.done'):
                    if added[index][1] != 'function_call' or index in argument_done:
                        raise _invalid('GPT inference stream contains inconsistent function arguments.')
                    fragment = data.get('delta' if kind.endswith('.delta') else 'arguments')
                    if type(fragment) is not str:
                        raise _invalid()
                    if kind.endswith('.delta'):
                        arguments[index] = arguments.get(index, '') + fragment
                    else:
                        if index in arguments and fragment != arguments[index]:
                            raise _invalid('GPT inference stream changed function arguments.')
                        arguments[index] = fragment
                        argument_done.add(index)
                if 'content_index' in data and (type(data['content_index']) is not int or not 0 <= data['content_index'] < 64):
                    raise _invalid()
        elif kind in ('response.output_item.added', 'response.output_item.done'):
            raise _invalid()
        if kind in ('response.failed', 'response.incomplete', 'error'):
            response = data.get('response')
            if kind == 'response.incomplete':
                if (type(response) is dict and type(response.get('incomplete_details')) is dict
                        and response['incomplete_details'].get('reason') == 'max_output_tokens'):
                    if response_id is not None and response.get('id') != response_id:
                        raise _invalid('GPT inference terminal response identity changed.')
                    final = _result(response, True, token)
                    continue
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


def validate_text_request(*, base_url, access_token, subscription, api_mode, request, _validate_instances=True):
    """Pure validation before the factory allocates its HTTP client."""
    if api_mode not in ('responses', 'completions') or (subscription and api_mode != 'responses'):
        raise ProviderAccessError('invalid_request', 'GPT subscription inference requires responses mode.')
    base_url = _endpoint(base_url, access_token, subscription)
    from .response_policy import prepare_response
    prepared, evidence = prepare_response(request, base_url=base_url, api_mode=api_mode, subscription=subscription)
    from api_agent.llm_providers.response_policy import format_payload
    body = format_payload(request, _payload(prepared, api_mode, subscription, _validate_instances=_validate_instances), evidence)
    if base_url == 'https://openrouter.ai/api/v1':
        # OpenRouter may reject otherwise tool-capable routes when this explicit
        # parameter is supplied. Returned calls remain independently validated.
        body.pop('parallel_tool_calls', None)
    if evidence is not None and evidence.actual_mode == 'json':
        if api_mode == 'responses':
            body['text'] = {'format': {'type': 'json_object'}}
        else:
            body['response_format'] = {'type': 'json_object'}
    if (base_url == 'https://openrouter.ai/api/v1' and evidence is not None
            and evidence.actual_mode == 'structured'):
        body['provider'] = {'require_parameters': True}
    from .reasoning import reasoning_fields
    body.update(reasoning_fields(request, base_url=base_url, api_mode=api_mode))
    if base_url == 'https://openrouter.ai/api/v1' and request.payload.generation.max_output_tokens is not None:
        body.setdefault('provider', {}).update(require_parameters=True, allow_fallbacks=False)
    from .rich import _protected
    from api_agent.providers.rich_inference import continuation
    if any(message.continuation is not None and _protected(continuation(message.continuation), access_token) for message in request.payload.messages):
        raise ProviderAccessError('invalid_request', 'GPT provider continuation contains protected authentication content.')
    body.update(prompt_cache_fields(request.payload.generation.cache_policy, request.selected_llm,
                                  base_url=base_url, subscription=subscription))
    if base_url == 'https://openrouter.ai/api/v1' and request.payload.generation.cache_policy in ('medium', 'high'):
        rows = body['input' if api_mode == 'responses' else 'messages']
        if not rows[-1].get('content'):
            raise ProviderAccessError('unsupported', 'OpenRouter enabled prompt-cache presets require a nonempty final text message.')
        rows[-1]['content'] = [{'type': 'input_text' if api_mode == 'responses' else 'text',
                               'text': rows[-1]['content'], 'prompt_cache_breakpoint': {'mode': 'explicit'}}]
    return base_url, body


async def async_validate_text_request(*, base_url, access_token, subscription, api_mode, request):
    from api_agent.providers.inference import async_request_to_wire
    from api_agent.providers.contracts import ContractError
    admitted = validate_text_request(base_url=base_url, access_token=access_token,
        subscription=subscription, api_mode=api_mode, request=request, _validate_instances=False)
    try:
        await async_request_to_wire(request)
    except ContractError:
        raise ProviderAccessError('invalid_request', 'Invalid GPT inference request.') from None
    return admitted


async def complete_text(*, client, base_url, access_token, subscription, api_mode, request, cache_session_id=None):
    """Perform one bounded physical inference attempt."""
    base_url, body = await async_validate_text_request(base_url=base_url, access_token=access_token,
        subscription=subscription, api_mode=api_mode, request=request)
    headers = {'Authorization': 'Bearer ' + access_token, 'Accept-Encoding': 'identity'}
    if subscription and cache_session_id is not None:
        try:
            if type(cache_session_id) is not str or str(UUID(cache_session_id)) != cache_session_id:
                raise ValueError
        except ValueError:
            raise ProviderAccessError('invalid_request', 'GPT subscription cache session must be a canonical UUID.') from None
        # ChatGPT's routing uses the HTTP session identity independently of the
        # JSON key. Both refer to this owned provider session, never credentials.
        body['prompt_cache_key'] = cache_session_id
        headers['session-id'] = cache_session_id
    from api_agent.providers.inference import error_usage
    from api_agent.workflows.inference_capture import record_response_failure
    failure_usage = None
    dispatch = {'response': None}
    try:
        async with component_timeout('GPT text inference', _TIMEOUT) as budget:
            try:
                return await _complete_attempt(client=client, base_url=base_url, headers=headers,
                    body=body, subscription=subscription, api_mode=api_mode, request=request,
                    access_token=access_token, budget=budget, dispatch=dispatch)
            except (OperationalError, TimeoutError, httpx.RequestError, CancelledError) as failure:
                failure_usage = error_usage(failure)
                record_response_failure(failure)
                raise
    except (ProviderAccessError, OperationalError, CancelledError) as error:
        error.response = dispatch['response']
        raise
    except (TimeoutError, httpx.TimeoutException) as failure:
        from api_agent.providers.inference import error_usage
        error = ProviderAccessError('deadline', 'GPT inference timed out.', usage=error_usage(failure) or failure_usage)
        error.execution = 'possibly_executed'
        error.response = dispatch['response']
        raise error from None
    except httpx.RequestError as failure:
        from api_agent.providers.inference import error_usage
        error = ProviderAccessError('unavailable', 'GPT inference transport is unavailable.', usage=error_usage(failure) or failure_usage)
        error.execution = 'possibly_executed'
        error.response = dispatch['response']
        raise error from None


def _admission_rejection(status, data, subscription):
    """Recognize a bounded admission envelope without execution evidence."""
    if not (subscription and status == 503 and type(data) is dict and set(data) == {'error'}
            and type(data['error']) is dict
            and set(data['error']) <= {'code', 'message', 'type', 'param'}
            and data['error'].get('code') == _RETRY_CODE):
        return False
    for name, limit in (('message', 512), ('type', 100), ('param', 200)):
        value = data['error'].get(name)
        if value is not None and (type(value) is not str or len(value) > limit):
            return False
    return True


async def _complete_attempt(*, client, base_url, headers, body, subscription, api_mode,
                            request, access_token, budget, dispatch):
    """Own and close one HTTP response before the caller decides to retry."""
    from .response_policy import prepare_response
    from api_agent.llm_providers.response_policy import capture_payload
    _, evidence = prepare_response(request, base_url=base_url, api_mode=api_mode, subscription=subscription)
    if budget.remaining == 0:
        raise TimeoutError('GPT inference deadline expired before dispatch.')
    capture_payload(request, body, evidence, auth=access_token)
    if budget.remaining == 0:
        raise TimeoutError('GPT inference deadline expired before dispatch.')
    if evidence is not None:
        from api_agent.workflows.inference_capture import record_response_dispatch
        record_response_dispatch(evidence)
        dispatch['response'] = evidence
    async with client.stream('POST', base_url + ('/responses' if api_mode == 'responses' else '/chat/completions'),
            json=body, headers=headers,
            follow_redirects=False, timeout=budget.remaining) as response:
        from api_agent.providers.inference_retry import retry_after
        status = response.status_code
        hint = retry_after(response.headers.get('retry-after'))
        try:
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
                error = _rejection(response.status_code, data, request_id, access_token,
                                   _authentication_type(response.headers.get('www-authenticate')), responses=api_mode == 'responses')
                if _admission_rejection(response.status_code, data, subscription):
                    error.execution = 'not_started'
                raise error
        except Exception as error:
            if status != 200:
                if not isinstance(error, OperationalError):
                    error = ProviderAccessError('unavailable', 'GPT inference was rejected.', status=status)
                    error.execution = 'possibly_executed'
                elif not isinstance(error, ProviderAccessError) or error.status is None:
                    error.execution = 'possibly_executed'
                error.http_status = status
                error.retry_after = hint
                raise error from None
            raise
        if subscription:
            if response.headers.get('content-type', '').split(';')[0].strip().lower() not in ('', 'text/event-stream'):
                raise _invalid('GPT subscription inference requires an event stream.')
            from .rich import async_validate_result
            result = replace(_sse(raw, access_token, request_id), response=evidence)
            from api_agent.providers.generation_attempts import snapshot_transport_result
            snapshot_transport_result(result)
            return await async_validate_result(request, result, access_token)
        data = _json(raw)
        result = _result(data, api_mode == 'responses', access_token)
        if base_url == 'https://openrouter.ai/api/v1':
            usage = _usage(data.get('usage'), api_mode == 'responses', router=True)
            try:
                billing = _billing(data, request_id, access_token)
            except ProviderAccessError as error:
                error.usage = usage
                raise
            result = replace(result, usage=usage, billing=billing)
        from .rich import async_validate_result
        result = replace(result, response=evidence)
        from api_agent.providers.generation_attempts import snapshot_transport_result
        snapshot_transport_result(result)
        return await async_validate_result(request, result, access_token)
