"""Actual outgoing requests retain one isolated, bounded dialogue prefix."""
import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import (CompletionRequest, GenerationOptions, LLMRequest,
    LLMResult, Message, ResponsePolicy, StructuredOutput, TextContent, Tool, ToolCall,
    ToolResult)
from api_agent.workflows.inference_history import InferenceHistory, inference_dialogue


def message(role, text):
    return Message(role, (TextContent(text),))


def request(text='initial', **options):
    messages = options.pop('messages', (message('system', 'Stable instructions'), message('user', text)))
    model = options.pop('model', 'selected-model')
    return LLMRequest(model, CompletionRequest(tuple(messages), **options), str(uuid4()))


def result(text='accepted response'):
    return LLMResult(message('assistant', text))


class Recorder:
    def __init__(self, responses=()):
        self.requests = []
        self.responses = list(responses)

    async def __call__(self, value, **kwargs):
        self.requests.append(value)
        return self.responses.pop(0) if self.responses else result()


@pytest.mark.asyncio
async def test_stateless_retry_appends_response_and_changed_input_without_rewriting_prefix():
    provider = Recorder([result('first candidate'), result('corrected candidate')])
    history = InferenceHistory()
    complete = history.services({'llm': provider}, 'draft')['llm']
    first = request('Original question')
    correction = request('Exact failure details\nPreserve Σ and correct the count.')

    await complete(first)
    answer = await complete(correction)

    assert provider.requests[0] == first
    assert provider.requests[1].payload.messages == (*first.payload.messages,
        message('assistant', 'first candidate'), correction.payload.messages[-1])
    assert provider.requests[1].request_id == correction.request_id
    assert answer.payload.content[0].text == 'corrected candidate'


@pytest.mark.asyncio
async def test_already_accumulated_native_tools_keep_exact_calls_results_and_opaque_state():
    tool = Tool('lookup', 'Read', '{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    assistant = Message('assistant', (), (ToolCall('one', 'lookup', '{}'),),
        continuation=json.dumps({'provider':'synthetic', 'state':[{'opaque':'retained-state'}]}))
    provider = Recorder([LLMResult(assistant, outcome='tool_calls'), result('found')])
    complete = InferenceHistory().services({'complete': provider}, 'conversation')['complete']
    first = request(tools=(tool,))
    actual = ToolResult('one', (TextContent('{"value":"unknown-until-read"}'),))

    await complete(first)
    following = request(messages=(*first.payload.messages, assistant,
        Message('tool', results=(actual,))), tools=(tool,))
    await complete(following)

    assert provider.requests[1] == following
    assert provider.requests[1].payload.messages[2] is assistant
    assert provider.requests[1].payload.messages[3].results == (actual,)


@pytest.mark.asyncio
async def test_native_conversation_can_extend_a_hidden_earlier_component_retry_prefix():
    provider = Recorder([result('old candidate'), result('new candidate'), result('final')])
    complete = InferenceHistory().services({'complete': provider}, 'node')['complete']
    first, corrected = request('old task'), request('corrected task')
    await complete(first)
    await complete(corrected)
    native_extension = request(messages=(*corrected.payload.messages,
        message('assistant', 'new candidate'), message('user', 'Actual follow-up result')))

    await complete(native_extension)

    previous = provider.requests[1].payload.messages
    assert provider.requests[2].payload.messages == (*previous,
        message('assistant', 'new candidate'), message('user', 'Actual follow-up result'))


@pytest.mark.asyncio
async def test_a_native_continuation_cannot_rewrite_an_accepted_response():
    provider = Recorder()
    complete = InferenceHistory().services({'llm': provider}, 'node')['llm']
    original = request()
    await complete(original)
    altered = request(messages=(*original.payload.messages, message('assistant', 'rewritten'),
        message('user', 'follow-up')))

    with pytest.raises(ContractError, match='rewrites'):
        await complete(altered)

    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_repeated_identical_input_repeats_only_the_last_user_update():
    provider = Recorder()
    complete = InferenceHistory().services({'llm': provider}, 'node')['llm']
    initial = request()
    await complete(initial)
    await complete(initial)

    assert provider.requests[1].payload.messages == (*initial.payload.messages,
        result().payload, initial.payload.messages[-1])


@pytest.mark.asyncio
async def test_system_only_repetition_rejects_without_inventing_a_user_instruction():
    provider = Recorder()
    complete = InferenceHistory().services({'llm': provider}, 'node')['llm']
    initial = request(messages=(message('system', 'Instruction only'),))
    await complete(initial)

    with pytest.raises(ContractError, match='user update'):
        await complete(initial)

    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['node', 'service', 'owner', 'model', 'schema', 'generation', 'system', 'tools', 'auth'])
async def test_distinct_node_or_request_authority_never_inherits_another_dialogue(change):
    provider, other = Recorder(), Recorder()
    history = InferenceHistory()
    original = request()
    authority = object()
    await history.call('plan', 'llm', provider, original, auth=authority)
    values = {'node':'plan', 'service':'llm', 'callback':provider, 'request':original}
    modifications = {
        'node': {'node':'review'}, 'service': {'service':'complete'}, 'owner': {'callback':other},
        'model': {'request':request(model='another-model')},
        'schema': {'request':request(structured=StructuredOutput('new', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'))},
        'generation': {'request':request(generation=GenerationOptions(reasoning_effort='low'))},
        'system': {'request':request(messages=(message('system', 'Different instructions'), message('user', 'initial')))},
        'tools': {'request':request(tools=(Tool('read', 'Read', '{"type":"object","properties":{},"required":[],"additionalProperties":false}'),))},
        'auth': {},
    }
    values.update(modifications[change])

    await history.call(**values, auth=object() if change == 'auth' else authority)

    recipient = other if change == 'owner' else provider
    assert recipient.requests[-1].payload.messages == values['request'].payload.messages


@pytest.mark.asyncio
async def test_explicit_verifier_lanes_and_nested_scopes_do_not_mix_tools():
    provider = Recorder()
    complete = InferenceHistory().services({'complete':provider}, 'node')['complete']
    for alias in ('primary', 'secondary'):
        with inference_dialogue('relevance'), inference_dialogue(alias):
            await complete(request('Verify candidate'))
    with inference_dialogue('relevance'), inference_dialogue('primary'):
        await complete(request('Correct primary candidate'))

    assert len(provider.requests[0].payload.messages) == len(provider.requests[1].payload.messages) == 2
    assert provider.requests[2].payload.messages[:2] == provider.requests[0].payload.messages
    assert provider.requests[2].payload.messages[-1].content[0].text == 'Correct primary candidate'


@pytest.mark.parametrize('name', [None, '', 1, 'x'*201, 'line\nbreak'])
def test_dialogue_names_are_bounded(name):
    with pytest.raises(ContractError, match='dialogue'):
        with inference_dialogue(name):
            pass


@pytest.mark.asyncio
async def test_failed_call_retains_request_and_safe_failure_without_fake_assistant_or_raw_error():
    seen = []
    async def provider(value):
        seen.append(value)
        if len(seen) == 1:
            raise ValueError('private server diagnostic must not become a prompt')
        return result()
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    with pytest.raises(ValueError, match='private server'):
        await complete(request())

    await complete(request('Caller-provided correction'))

    messages = seen[1].payload.messages
    assert all(m.role != 'assistant' for m in messages)
    assert json.loads(messages[-2].content[0].text) == {
        'previous_inference':'failed', 'accepted_response':False, 'execution':'unknown'}
    assert 'private server' not in repr([part.text for m in messages for part in m.content])


@pytest.mark.asyncio
async def test_cancelled_queue_waiter_never_dispatches_and_does_not_release_the_active_owner():
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def provider(value):
        seen.append(value)
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return result()
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    first = asyncio.create_task(complete(request('one')))
    await entered.wait()
    cancelled = asyncio.create_task(complete(request('cancelled')))
    await asyncio.sleep(0)
    third = asyncio.create_task(complete(request('three')))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(seen) == 1
    release.set()
    await first
    await third

    assert len(seen) == 2
    assert seen[-1].payload.messages[-1].content[0].text == 'three'
    assert all(part.text != 'cancelled' for m in seen[-1].payload.messages for part in m.content)


@pytest.mark.asyncio
async def test_history_queue_is_usable_by_a_sync_nodes_separate_event_loop():
    provider = Recorder()
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    await complete(request('one'))

    await asyncio.to_thread(lambda: asyncio.run(complete(request('two'))))

    assert provider.requests[1].payload.messages[:2] == provider.requests[0].payload.messages


@pytest.mark.asyncio
@pytest.mark.parametrize('error,category', [(TimeoutError('deadline'), 'timeout'), (asyncio.CancelledError(), 'cancelled')])
async def test_terminal_cancellation_and_timeout_keep_original_failure_and_release_ownership(error, category):
    seen = []
    async def provider(value):
        seen.append(value)
        if len(seen) == 1:
            raise error
        return result()
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    with pytest.raises(type(error)):
        await complete(request())
    await complete(request('next user action'))

    assert json.loads(seen[-1].payload.messages[-2].content[0].text)['previous_inference'] == category


@pytest.mark.parametrize('limits', [{'max_bytes':0}, {'max_bytes':True}, {'max_dialogues':0}, {'max_dialogues':1.5}])
def test_invalid_retention_bounds_reject(limits):
    with pytest.raises(ValueError, match='bounds'):
        InferenceHistory(**limits)


@pytest.mark.asyncio
async def test_invocation_and_dialogue_budgets_reject_before_dispatch():
    provider = Recorder()
    with pytest.raises(ContractError, match='budget'):
        await InferenceHistory(max_bytes=1).call('node', 'llm', provider, request())
    assert not provider.requests
    history = InferenceHistory(max_dialogues=1)
    await history.call('one', 'llm', provider, request())
    with pytest.raises(ContractError, match='budget'):
        await history.call('two', 'llm', provider, request())
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_an_unretainable_completed_response_is_returned_but_further_dispatch_is_blocked():
    provider = Recorder([result('x'*4000)])
    history = InferenceHistory(max_bytes=1000)
    complete = history.services({'llm':provider}, 'node')['llm']

    actual = await complete(request())
    with pytest.raises(ContractError, match='budget'):
        await complete(request('retry'))

    assert actual.payload.content[0].text == 'x'*4000
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_retention_budget_counts_current_history_rather_than_every_obsolete_snapshot():
    provider = Recorder()
    complete = InferenceHistory(max_bytes=8192).services({'llm':provider}, 'node')['llm']
    for index in range(20):
        await complete(request(str(index)))

    assert len(provider.requests) == 20
    assert provider.requests[-1].payload.messages[:2] == provider.requests[0].payload.messages


@pytest.mark.asyncio
async def test_history_does_not_trim_to_fit_the_shared_message_bound():
    provider = Recorder()
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    for index in range(32):
        await complete(request(str(index)))

    with pytest.raises(ContractError, match='64-message'):
        await complete(request('one too many'))

    assert len(provider.requests) == 32
    assert len(provider.requests[-1].payload.messages) == 64


@pytest.mark.asyncio
async def test_history_does_not_trim_to_fit_the_shared_byte_bound():
    provider = Recorder([result('a'*500000)])
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    original = request(messages=(message('user','x'*500000), message('user','y'*500000), message('user','z'*500000)))
    await complete(original)
    following = request(messages=(*original.payload.messages[:2], message('user','next'*125000)))

    with pytest.raises(ContractError, match='byte limit'):
        await complete(following)

    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_closed_invocation_releases_history_but_does_not_dispose_borrowed_services():
    provider = Recorder()
    history = InferenceHistory()
    values = {'llm':provider, 'complete':None, 'state':0}
    services = history.services(values, 'node')
    services['state'] = 1
    assert values['state'] == 1 and services['complete'] is None
    assert set(services) == set(values) and len(services) == 3
    del services['complete']
    assert services['state'] == 1 and any(callable(value) for value in services.values())
    complete = services['llm']
    await complete(request())
    history.close()

    with pytest.raises(ContractError, match='closed'):
        await complete(request('after close'))
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_non_llm_complete_services_keep_their_original_sync_or_async_contract():
    async def asynchronous(value):
        return value + 1
    history = InferenceHistory()
    sync = history.services({'complete':lambda value:value+1}, 'sync')['complete']
    asynchronous = history.services({'complete':asynchronous}, 'async')['complete']

    assert await sync(4) == await asynchronous(4) == 5


@pytest.mark.asyncio
async def test_invalid_llm_result_fails_without_manufacturing_an_assistant_message():
    history = InferenceHistory()
    complete = history.services({'llm':lambda value:'not an LLMResult'}, 'node')['llm']
    with pytest.raises(ContractError, match='invalid result'):
        await complete(request())


@pytest.mark.asyncio
async def test_closing_while_a_call_is_running_returns_its_result_but_blocks_queued_dispatch():
    history = InferenceHistory()
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def provider(value):
        seen.append(value)
        entered.set()
        await release.wait()
        history.close()
        return result('completed before disposal')
    complete = history.services({'llm':provider}, 'node')['llm']
    active = asyncio.create_task(complete(request()))
    await entered.wait()
    queued = asyncio.create_task(complete(request('queued')))
    await asyncio.sleep(0)
    release.set()

    assert (await active).payload.content[0].text == 'completed before disposal'
    with pytest.raises(ContractError, match='closed'):
        await queued
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_ephemeral_auth_identity_is_retained_until_history_closes_to_prevent_id_reuse():
    import gc
    import weakref
    class Authority:
        pass
    authority = Authority()
    reference = weakref.ref(authority)
    history = InferenceHistory()
    await history.call('node', 'llm', Recorder(), request(), auth=authority)
    del authority
    gc.collect()
    assert reference() is not None

    history.close()
    gc.collect()

    assert reference() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate_in_batch', [False, True])
async def test_replayed_tool_call_ids_fail_before_becoming_an_execution_proposal(duplicate_in_batch):
    tool = Tool('lookup', 'Read', '{"type":"object","properties":{},"required":[],"additionalProperties":false}')
    old = Message('assistant', calls=(ToolCall('old', 'lookup', '{}'),))
    completed = Message('tool', results=(ToolResult('old', (TextContent('actual value'),)),))
    calls = (ToolCall('new', 'lookup', '{}'),)*2 if duplicate_in_batch else (ToolCall('old', 'lookup', '{}'),)
    bad = LLMResult(Message('assistant', calls=calls), outcome='tool_calls')
    provider = Recorder([bad])
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']
    initial = request(messages=(message('user','Read'), old, completed), tools=(tool,))

    with pytest.raises(ProviderAccessError) as caught:
        await complete(initial)

    assert caught.value.code == 'tool_call_replay'
    assert caught.value.origin == 'local_output_validation'
    assert caught.value.usage is bad.usage
    assert caught.value.execution == 'completed'
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_unrecognized_error_metadata_does_not_mask_the_original_exception():
    failure = ValueError('original error')
    failure.response = {'not':'response evidence'}
    async def provider(value):
        raise failure
    policy = ResponsePolicy('structured','allow', StructuredOutput('shape',
        '{"type":"object","properties":{},"required":[],"additionalProperties":false}'))

    with pytest.raises(ValueError) as caught:
        await InferenceHistory().services({'llm':provider}, 'node')['llm'](request(response=policy))

    assert caught.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize('transport,model', [
    ('gpt-responses','gpt-example'), ('gpt-completions','gpt-example'),
    ('runtime-responses','us.openai.gpt-6-luna'), ('runtime-completions','us.openai.gpt-6-luna'),
    ('mantle-completions','zai.glm-5'),
    ('converse','us.anthropic.claude-haiku-4-5-20251001-v1:0')])
@pytest.mark.parametrize('actual_mode', ['structured','prompted_json'])
async def test_actual_transport_prefix_survives_native_and_formatted_json_retries(transport, model, actual_mode):
    from api_agent.llm_providers.response_policy import resolve_response, format_payload
    from api_agent.llm_providers.gpt.rich import payload as gpt_payload
    from api_agent.llm_providers.bedrock import converse, mantle
    bodies = []
    policy = ResponsePolicy('structured','allow', StructuredOutput('shape',
        '{"type":"object","properties":{},"required":[],"additionalProperties":false}'))
    async def provider(value):
        prepared, evidence = resolve_response(value, lambda mode:None if mode == actual_mode else 'unsupported_mode')
        if transport == 'converse':
            body = converse.format_payload(value, converse.encode(prepared), evidence)
        else:
            owner, mode = transport.split('-')
            body = (gpt_payload(prepared, mode, False) if owner == 'gpt' else
                    mantle.encode(prepared, mode, endpoint='runtime' if owner == 'runtime' else 'mantle'))
            body = format_payload(value, body, evidence)
        bodies.append(body)
        return replace(result('{}'), response=evidence)
    complete = InferenceHistory().services({'llm':provider}, 'node')['llm']

    await complete(request('initial', model=model, response=policy))
    await complete(request('specific correction', model=model, response=policy))

    container = 'input' if 'input' in bodies[0] else 'messages'
    assert bodies[1][container][:len(bodies[0][container])] == bodies[0][container]
    assert {k:v for k,v in bodies[0].items() if k != container} == {
        k:v for k,v in bodies[1].items() if k != container}


def test_converse_fallback_instruction_marker_identifies_only_the_appended_content_block(monkeypatch):
    from api_agent.llm_providers.response_policy import resolve_response, capture_payload
    from api_agent.llm_providers.bedrock import converse
    policy = ResponsePolicy('structured','allow', StructuredOutput('shape',
        '{"type":"object","properties":{},"required":[],"additionalProperties":false}'))
    value = request('authored prompt', model='us.anthropic.claude-haiku-4-5-20251001-v1:0', response=policy)
    prepared, evidence = resolve_response(value, lambda mode:None if mode == 'prompted_json' else 'unsupported_mode')
    body = converse.format_payload(value, converse.encode(prepared), evidence)
    seen = []
    monkeypatch.setattr('api_agent.workflows.inference_capture.capture_inference', lambda *args, **kwargs:seen.append(args[1]))

    capture_payload(value, body, evidence, auth=None)

    assert seen[0]['adapter_instructions'] == [
        {'role':'user','path':'/messages/0/content/1','purpose':'response_policy'}]
    assert body['messages'][0]['content'][0] == {'text':'authored prompt'}


def test_converse_fallback_after_an_assistant_appends_a_new_user_message_without_mutating_body():
    from api_agent.llm_providers.response_policy import resolve_response
    from api_agent.llm_providers.bedrock import converse
    policy = ResponsePolicy('structured','allow', StructuredOutput('shape',
        '{"type":"object","properties":{},"required":[],"additionalProperties":false}'))
    value = request(messages=(message('user','original'), message('assistant','previous')),
        model='us.anthropic.claude-haiku-4-5-20251001-v1:0', response=policy)
    prepared, evidence = resolve_response(value, lambda mode:None if mode == 'prompted_json' else 'unsupported_mode')
    original = converse.encode(prepared)

    formatted = converse.format_payload(value, original, evidence)

    assert len(formatted['messages']) == len(original['messages']) + 1
    assert formatted['messages'][:-1] == original['messages']
    assert original['messages'][-1] == {'role':'assistant','content':[{'text':'previous'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('argument_form', ['positional', 'provider_keyword'])
async def test_provider_owned_authority_arguments_partition_and_retain_history(argument_form):
    seen = []
    async def provider(value, *args, **kwargs):
        seen.append(value)
        return result()
    complete = InferenceHistory().services({'llm': provider}, 'node')['llm']
    first_authority, second_authority = object(), object()
    async def invoke(authority, text):
        if argument_form == 'positional':
            return await complete(request(text), authority)
        return await complete(request(text), credentials=authority)

    await invoke(first_authority, 'Private first prompt')
    await invoke(second_authority, 'Separate second prompt')
    await invoke(first_authority, 'First identity correction')

    assert len(seen[1].payload.messages) == 2
    assert seen[1].payload.messages[-1].content[0].text == 'Separate second prompt'
    assert seen[2].payload.messages[:2] == seen[0].payload.messages
    assert seen[2].payload.messages[2].role == 'assistant'
    assert all('Separate second prompt' not in part.text
               for row in seen[2].payload.messages for part in row.content)
