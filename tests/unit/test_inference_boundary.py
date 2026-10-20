"""Text boundary rejects unsupported/oversized values before executing a provider."""
from copy import deepcopy
from dataclasses import replace
import pytest
from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import request_from_wire, request_to_wire, result_from_wire, result_to_wire
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Tool, ToolCall, ToolResult, StructuredOutput, Usage


def request_row():
    return dict(selected_llm='gpt-example', request_id='request', messages=[{'role': 'user', 'text': 'Ok'}], max_output_tokens=128, cache_policy=None)


def result_row():
    return dict(text='Ok', outcome='completed', usage=dict(input_tokens=2, output_tokens=1, cached_input_tokens=1, reasoning_tokens=0))


def test_exact_round_trips_preserve_text_usage_and_outcomes():
    row = request_row()
    row['messages'] = [dict(role=role, text='Line 1\nLine 2') for role in ('system','user','assistant')]
    value = request_from_wire(row)
    assert request_to_wire(value) == row and value.payload.generation.max_output_tokens == 128
    for outcome in ('completed', 'length', 'refused'):
        row = result_row() | {'outcome': outcome}
        value = result_from_wire(row)
        assert result_to_wire(value) == row
        assert value.usage.cached_input_tokens == 1 and value.payload.content[0].text == 'Ok'


@pytest.mark.parametrize('patch', [None, [], {}, request_row() | {'extra': 1}, request_row() | {'selected_llm': ''},
    request_row() | {'selected_llm': 1}, request_row() | {'selected_llm': 'x'*201}, request_row() | {'selected_llm': '\n'},
    request_row() | {'request_id': '\n'}, request_row() | {'request_id': True}, request_row() | {'request_id': 'x'*201},
     request_row() | {'max_output_tokens': True}, request_row() | {'max_output_tokens': 0},
    request_row() | {'max_output_tokens': 1048577}, request_row() | {'messages': ()}, request_row() | {'messages': []},
    request_row() | {'messages': [None]}, request_row() | {'messages': [{}]}, request_row() | {'messages': [{'role': 'tool', 'text': 'x'}]},
    request_row() | {'messages': [{'role': 1, 'text': 'x'}]}, request_row() | {'messages': [{'role': 'user', 'text': 1}]},
    request_row() | {'messages': [{'role': 'user', 'text': 'x'*524289}]}, request_row() | {'messages': [{'role': 'user', 'text':'x'}]*65}])
def test_invalid_wire_requests_rejected(patch):
    with pytest.raises(ContractError):
        request_from_wire(patch)


@pytest.mark.parametrize('value', [None, 'payload',
    LLMRequest('gpt-example', CompletionRequest((Message('user',(TextContent('x'),)),), structured=StructuredOutput('x','{}'))),
    LLMRequest('gpt-example', CompletionRequest((Message('user',(TextContent('x'),)),), tools=(Tool('x','x','{}'),))),
    LLMRequest('gpt-example', CompletionRequest((Message('assistant',(TextContent('x'),),calls=(ToolCall('id','x','{}'),)),))),
    LLMRequest('gpt-example', CompletionRequest((Message('tool',results=(ToolResult('id',(TextContent('x'),)),)),))),
    LLMRequest('gpt-example', CompletionRequest((Message('user'),))),
    LLMRequest('gpt-example', CompletionRequest((Message('user',(TextContent('x'),TextContent('y'))),)))])
def test_unsupported_request_objects_fail(value):
    with pytest.raises(ContractError):
        request_to_wire(value)


@pytest.mark.parametrize('row', [None, [], {}, result_row() | {'extra': 1}, result_row() | {'text': 1}, result_row() | {'text': 'x'*524289},
    result_row() | {'outcome': 'tool_calls'}, result_row() | {'outcome': True}, result_row() | {'usage': None}, result_row() | {'usage': {}},
    result_row() | {'usage': result_row()['usage'] | {'output_tokens': True}}, result_row() | {'usage': result_row()['usage'] | {'output_tokens': -1}},
    result_row() | {'usage': result_row()['usage'] | {'output_tokens': 2147483648}},
    result_row() | {'usage': result_row()['usage'] | {'cached_input_tokens': 3}},
    result_row() | {'usage': result_row()['usage'] | {'reasoning_tokens': 2}}])
def test_invalid_results_rejected(row):
    with pytest.raises(ContractError):
        result_from_wire(row)


@pytest.mark.parametrize('value', [None, 'secret', LLMResult(Message('assistant')), LLMResult(Message('assistant',(TextContent('x'),),calls=(ToolCall('id','x','{}'),))),
    LLMResult(Message('assistant',(TextContent('x'),TextContent('y'))))])
def test_invalid_provider_results_rejected(value):
    with pytest.raises(ContractError):
        result_to_wire(value)


def test_total_wire_size_checked_before_encoding_cross_process_frame():
    row = request_row() | {'messages': [{'role': 'user', 'text': '\u20ac'*524288}]}
    with pytest.raises(ContractError, match='byte limit'):
        request_from_wire(row)
    with pytest.raises(ContractError, match='byte limit'):
        result_from_wire(result_row() | {'text': '\u20ac'*524288})
    assert request_to_wire(request_from_wire(request_row() | {'max_output_tokens': None}))['max_output_tokens'] is None
    assert request_to_wire(request_from_wire(request_row() | {'request_id': ''}))['request_id'] == ''
    assert result_from_wire(result_row() | {'usage': {key: None for key in result_row()['usage']}}).usage == Usage()
