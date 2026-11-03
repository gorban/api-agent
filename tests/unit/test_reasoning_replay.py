"""Completed answers keep opaque provider state without publishing it to UI."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from api_agent.api.chat_contract import result as browser_result
from api_agent.llm_providers.bedrock import mantle
from api_agent.llm_providers.gpt.inference import _result, _payload
from api_agent.llm_providers.gpt.rich import native_summary, payload
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_from_wire, request_to_wire, result_from_wire, result_to_wire
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent
from api_agent.workflows.inference_history import InferenceHistory
from test_bedrock_mantle import completion, message, response, MODEL


def native():
    return [{'type': 'reasoning', 'id': 'r1', 'summary': [{'type': 'summary_text', 'text': 'Checked.'}],
             'encrypted_content': 'opaque-state'}, message('Ok')]


@pytest.mark.parametrize('provider', ['bedrock', 'gpt'])
def test_completed_answer_preserves_exact_native_items_but_browser_never_receives_state(provider):
    raw = response(native())
    result = mantle.decode(raw, 'responses', selected_llm=MODEL) if provider == 'bedrock' else _result(raw, True, 'credential-sentinel')
    assert result.reasoning_summary == 'Checked.'
    assert json.loads(result.payload.continuation)['state'] == native()
    assert result_from_wire(result_to_wire(result)) == result
    shown = browser_result('turn', result)
    assert shown['response_text'] == 'Ok' and shown['reasoning_summary'] == 'Checked.'
    assert 'opaque-state' not in json.dumps(shown) and 'continuation' not in json.dumps(shown)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['bedrock', 'gpt'])
async def test_workflow_retry_replays_completed_native_answer_in_original_order(provider):
    raw = response(native())
    answer = mantle.decode(raw, 'responses', selected_llm=MODEL) if provider == 'bedrock' else _result(raw, True, 'credential-sentinel')
    sent = []
    async def complete(value):
        sent.append(value)
        return answer
    service = InferenceHistory().services({'llm': complete}, 'node')['llm']
    first = LLMRequest(MODEL if provider == 'bedrock' else 'gpt-6-luna', CompletionRequest((Message('user', (TextContent('First.'),)),)))
    await service(first)
    await service(replace(first, payload=replace(first.payload, messages=(Message('user', (TextContent('Correct this.'),)),))))
    assert sent[1].payload.messages[1] is answer.payload
    assert request_from_wire(request_to_wire(sent[1])) == sent[1]
    body = mantle.encode(sent[1], 'responses') if provider == 'bedrock' else _payload(sent[1], 'responses', False)
    assert body['input'][1:3] == native()
    assert body['input'][3] == {'role': 'user', 'content': 'Correct this.'}


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_plaintext_completions_reasoning_is_replayed_without_reformatting(field):
    raw = completion()
    raw['choices'][0]['message'][field] = 'First sentence. Second sentence.\nNext line.'
    answer = mantle.decode(raw, 'completions', selected_llm=MODEL)
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('First'),)), answer.payload,
                                                  Message('user', (TextContent('Next'),)))))
    encoded = mantle.encode(request, 'completions')
    assert encoded['messages'][1] == raw['choices'][0]['message']
    assert result_from_wire(result_to_wire(answer)) == answer


@pytest.mark.parametrize('text', ['<reasoning>Check 17 × 19.</reasoning>Ok', '<think></think>Ok'])
def test_explicit_tagged_completion_replays_the_original_prefix_without_display_projection(text):
    raw = completion()
    raw['choices'][0]['message']['content'] = text
    answer = mantle.decode(raw, 'completions', selected_llm=MODEL, reasoning_format='tagged')
    assert answer.payload.content[0].text == 'Ok'
    assert mantle.continuation_state(answer.payload, 'completions') == [raw['choices'][0]['message']]


@pytest.mark.parametrize('change', [
    {'role': 'user'}, {'annotations': [{}]}, {'reasoning': ''}, {'reasoning': 4},
    {'content': 'different'}, {'tool_calls': [{'bad': 'shape'}]}, {'unexpected': True}])
def test_completions_replay_rejects_invalid_or_rewritten_state(change):
    raw = completion()
    raw['choices'][0]['message']['reasoning'] = 'Checked.'
    answer = mantle.decode(raw, 'completions', selected_llm=MODEL)
    envelope = json.loads(answer.payload.continuation)
    envelope['state'][0].update(change)
    altered = replace(answer.payload, continuation=json.dumps(envelope))
    with pytest.raises(ProviderAccessError):
        mantle.continuation_state(altered, 'completions')


@pytest.mark.parametrize('rows', [None, {}, [{'type':'summary_text', 'text': 4}],
    [{'type':'reasoning_text', 'text':'wrong carrier'}], [{'type':'summary_text', 'text':'x'*65537}]])
def test_gpt_summary_rejects_malformed_or_oversized_content(rows):
    with pytest.raises(ContractError):
        native_summary([{'type':'reasoning', 'summary':rows}])


@pytest.mark.parametrize('field,value', [('summary', {}), ('encrypted_content', ''), ('content', [{'type':'reasoning_text','text':'private'}])])
def test_gpt_completed_native_state_is_validated_before_return(field, value):
    raw = response(native())
    raw['output'][0][field] = value
    with pytest.raises(ProviderAccessError):
        _result(raw, True, 'credential-sentinel')


@pytest.mark.parametrize('content', [None, []])
def test_gpt_completed_reasoning_accepts_absent_semantics_in_optional_content(content):
    raw = response(native())
    raw['output'][0]['content'] = content
    result = _result(raw, True, 'credential-sentinel')
    assert json.loads(result.payload.continuation)['state'] == raw['output']


def test_bedrock_plaintext_responses_reasoning_can_display_without_a_native_summary():
    raw = response(native())
    raw['output'][0].update(summary=[],content=[{'type':'reasoning_text','text':'Checked publicly.'}])
    result = mantle.decode(raw,'responses',selected_llm=MODEL)
    assert result.reasoning_summary == 'Checked publicly.'
    assert json.loads(result.payload.continuation)['state'] == raw['output']


@pytest.mark.parametrize('first,second',[('secret-','canary'),('canary','secret-')])
def test_undisplayed_native_replay_text_cannot_split_a_credential_with_the_answer(first,second):
    from api_agent.llm_providers.bedrock.normalized_stream import guard_result
    raw=response(native())
    raw['output'][0]['content']=[{'type':'reasoning_text','text':first}]
    raw['output'][-1]=message(second)
    result=mantle.decode(raw,'responses',selected_llm=MODEL)
    assert result.reasoning_summary == 'Checked.'
    with pytest.raises(ContractError):
        guard_result(result,{'secret-canary'})


def test_bedrock_native_replay_rejects_reasoning_with_no_replayable_state():
    raw = native()
    raw[0].update(encrypted_content=None,summary=[],content=[])
    with pytest.raises(ProviderAccessError):
        mantle.native_state(raw)


def converse_result():
    from api_agent.llm_providers.bedrock import converse
    from test_bedrock_converse import response, MODEL as selected
    raw=response()
    raw['output']['message']['content'].insert(0,{'reasoningContent':{
        'reasoningText':{'text':'Checked.','signature':'opaque-signature'}}})
    return converse.decode(raw,selected_llm=selected),raw,selected


def test_completed_converse_replays_signature_and_original_blocks_without_showing_signature():
    from api_agent.llm_providers.bedrock import converse
    answer,raw,selected=converse_result()
    request=LLMRequest(selected,CompletionRequest((Message('user',(TextContent('Hello'),)),answer.payload,
        Message('user',(TextContent('Next'),)))))
    assert converse.encode(request)['messages'][1] == raw['output']['message']
    assert 'opaque-signature' not in json.dumps(browser_result('turn',answer))
    assert result_from_wire(result_to_wire(answer)) == answer


@pytest.mark.parametrize('case',['owner','extra-item','malformed-native','no-reasoning','visible-mismatch'])
def test_converse_replay_rejects_unowned_or_inconsistent_state(case):
    from api_agent.llm_providers.bedrock import converse
    answer,raw,selected=converse_result()
    envelope=json.loads(answer.payload.continuation)
    if case=='owner':envelope['provider']='other'
    elif case=='extra-item':envelope['state']*=2
    elif case=='malformed-native':envelope['state'][0]={'role':'user','content':[]}
    elif case=='no-reasoning':envelope['state'][0]['content']=[{'text':'hello'}]
    else:envelope['state'][0]['content'][-1]['text']='changed'
    with pytest.raises(ProviderAccessError,match='Invalid Bedrock Converse continuation'):
        converse.continuation_message(replace(answer.payload,continuation=json.dumps(envelope)),selected)


def test_gpt_summary_credential_is_not_released():
    raw = response(native())
    with pytest.raises(ProviderAccessError, match='protected'):
        _result(raw, True, 'Checked.')


def test_gpt_summary_invisible_credential_characters_are_not_released():
    raw = response(native())
    raw['output'][0]['summary'][0]['text'] = 'cred\u200bential'
    with pytest.raises(ProviderAccessError, match='protected'):
        _result(raw, True, 'credential')


def test_tagged_completions_tool_response_replays_original_reasoning_and_calls():
    from api_agent.providers.llm import Tool, ToolResult
    from test_bedrock_mantle import call
    raw = completion()
    original = {'role':'assistant', 'content':'<reasoning>Plan.</reasoning>', 'tool_calls':[call()]}
    raw['choices'][0].update(message=original, finish_reason='tool_calls')
    answer = mantle.decode(raw, 'completions', selected_llm=MODEL, reasoning_format='tagged')
    assert answer.reasoning_summary == 'Plan.'
    request = LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('Find.'),)), answer.payload,
        Message('tool', results=(ToolResult('c', (TextContent('Found.'),)),))),
        tools=(Tool('lookup','Read','{"type":"object","properties":{},"required":[],"additionalProperties":false}'),)))
    assert mantle.encode(request, 'completions')['messages'][1] == original


@pytest.mark.parametrize('rich', [False, True])
def test_opt_in_display_request_roundtrips_without_changing_default_requests(rich):
    from api_agent.providers.llm import ResponsePolicy
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Hello'),)),),
        generation=GenerationOptions(display_summary=True), response=ResponsePolicy('text', 'reject') if rich else None))
    assert request_from_wire(request_to_wire(request)) == request


@pytest.mark.parametrize('value', [None, 1, 'true'])
def test_display_request_hint_is_strictly_boolean(value):
    with pytest.raises(ValueError):
        GenerationOptions(display_summary=value)
    request = LLMRequest('selected',CompletionRequest((Message('user',(TextContent('Hello'),)),)))
    with pytest.raises(ContractError):
        request_from_wire({**request_to_wire(request),'display_summary':value})


def test_official_native_summary_request_is_opt_in_and_route_scoped():
    from api_agent.llm_providers.gpt.reasoning import reasoning_fields
    request = LLMRequest('gpt-6-luna', CompletionRequest((Message('user', (TextContent('Hello'),)),),
        generation=GenerationOptions(display_summary=True)))
    assert reasoning_fields(request, base_url='https://api.openai.com/v1', api_mode='responses') == {'reasoning': {'summary':'auto'}}
    assert reasoning_fields(request, base_url='https://api.openai.com/v1', api_mode='completions') == {}
    assert reasoning_fields(request, base_url='https://elsewhere.test/v1', api_mode='responses') == {}
    from api_agent.llm_providers.gpt.reasoning import display_reasoning
    assert display_reasoning('gpt-6-luna',base_url='https://api.openai.com/v1',api_mode='responses')
    assert not display_reasoning('gpt-6-sol',base_url='https://api.openai.com/v1',api_mode='responses')


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_bedrock_provider_binds_completions_state_before_inference_and_replay(monkeypatch, streaming):
    from types import SimpleNamespace
    from api_agent.llm_providers.bedrock import factory as module
    from api_agent.llm_providers.bedrock.normalized_stream import WorkerStream
    from test_bedrock_mantle import completion_stream, chunk
    credentials = {'mode':'temporary','access_key':'synthetic-access','secret_key':'synthetic-secret',
                   'session_token':'synthetic-token','region':'us-east-1'}
    async def auth():
        return SimpleNamespace(worker_config=lambda:dict(credentials))
    class Transport:
        async def call(self, operation, payload, *, auth, join, on_dispatch):
            assert (await auth()).worker_config() == credentials
            on_dispatch()
            raw = completion()
            raw['choices'][0]['message']['reasoning'] = 'Checked.'
            return raw
        async def stream(self, operation, payload, *, auth, join, on_dispatch):
            assert (await auth()).worker_config() == credentials
            on_dispatch()
            decoder = WorkerStream('completions',MODEL,set())
            events = list(completion_stream())
            events.insert(1,chunk({'reasoning':'Checked.'}))
            for event in events:
                for frame in decoder.feed(event):
                    yield frame
            for frame in decoder.finish():
                yield frame
        async def aclose(self):
            pass
    monkeypatch.setattr(module, 'SDKTransport', lambda **options:Transport())
    provider = module.factory({'BEDROCK_ENDPOINT':'mantle','BEDROCK_API_MODE':'completions'}, {})
    request = LLMRequest(MODEL, CompletionRequest((Message('user',(TextContent('Hi'),)),)))
    try:
        if streaming:
            answer = [event async for event in provider.astream(request,auth=auth)][-1].payload
        else:
            answer = await provider.acomplete(request,auth=auth)
        assert json.loads(answer.payload.continuation)['provider'].startswith('bedrock.completions.')
        following = replace(request,payload=replace(request.payload,messages=(*request.payload.messages,
            answer.payload,Message('user',(TextContent('Next'),)))))
        assert (await provider.acomplete(following,auth=auth)).payload.content[0].text == 'hi'
    finally:
        await provider.aclose()
