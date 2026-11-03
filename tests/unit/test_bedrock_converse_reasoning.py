"""Converse controls, bounded binary replay and public projection contracts."""
from copy import deepcopy
from dataclasses import replace
import base64
import json

import pytest

from api_agent.llm_providers.bedrock import converse as codec
from api_agent.llm_providers.bedrock import converse_controls as controls
from api_agent.llm_providers.bedrock import converse_state as state
from api_agent.llm_providers.bedrock.catalog import selection
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent, ToolResult, Tool
from test_bedrock_converse import response

GPT = 'us.openai.gpt-6-luna'
CLAUDE = 'us.anthropic.claude-sonnet-5-5'
BUDGET = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0'
OPUS = 'us.anthropic.claude-opus-4-5-20251101-v1:0'


def request(model=GPT, effort='high', *, maximum=8192, display=False, temperature=None):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('Question'),)),),
        generation=GenerationOptions(max_output_tokens=maximum, reasoning_effort=effort,
            display_summary=display, temperature=temperature)))


@pytest.mark.parametrize('model', [GPT, CLAUDE, BUDGET, OPUS, 'openai.gpt-oss-20b-1:0'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_catalog_admits_controls_that_encode_into_the_worker_contract(model, effort):
    selected = selection(model, 'converse', endpoint='runtime')
    assert all(effort in selected.reasoning_efforts for effort in ('low', 'medium', 'high'))
    assert 'display_reasoning' in selected.features
    body = codec.encode(request(model, effort))
    controls.validate_fields(body, selected)
    assert body['additionalModelRequestFields']


@pytest.mark.parametrize('effort', [None, ''])
@pytest.mark.parametrize('model', [GPT, CLAUDE, BUDGET])
def test_omitted_effort_retains_provider_defaults(model, effort):
    assert 'additionalModelRequestFields' not in codec.encode(request(model, effort))


@pytest.mark.parametrize('model, expected', [
    (GPT, {'reasoning': {'effort': 'high', 'summary': 'auto'}}),
    (CLAUDE, {'thinking': {'type': 'adaptive', 'display': 'summarized'}, 'output_config': {'effort': 'high'}}),
])
def test_native_effort_and_summary_serialization(model, expected):
    body = codec.encode(request(model, display=True), config={'LLM_REASONING_HIGH_TOKENS': 'unused'})
    assert body['additionalModelRequestFields'] == expected


@pytest.mark.parametrize('model', [GPT, CLAUDE])
def test_native_summary_can_use_provider_default_effort(model):
    body = codec.encode(request(model, None, display=True))
    controls.validate_fields(body, selection(model, 'converse', 'runtime'))
    assert 'effort' not in body['additionalModelRequestFields'].get('output_config', {})
    assert 'reasoning_effort' not in body['additionalModelRequestFields']


@pytest.mark.parametrize('effort, expected', [('low', 2048), ('medium', 8192), ('high', 10000)])
def test_configured_budget_presets_reserve_answer_headroom(effort, expected):
    body = codec.encode(request(BUDGET, effort, maximum=11024), config={
        'LLM_REASONING_LOW_TOKENS': 2048, 'LLM_REASONING_MEDIUM_TOKENS': 8192,
        'LLM_REASONING_HIGH_TOKENS': 20000})
    assert body['additionalModelRequestFields'] == {'thinking': {'type': 'enabled', 'budget_tokens': expected}}
    assert body['inferenceConfig']['maxTokens'] == 11024


def test_opus45_composes_native_effort_with_its_required_manual_budget():
    body = codec.encode(request(OPUS, 'low'))
    assert body['additionalModelRequestFields'] == {
        'thinking': {'type': 'enabled', 'budget_tokens': 1024}, 'output_config': {'effort': 'low'},
        'anthropic_beta': ['effort-2025-11-24']}


@pytest.mark.parametrize('config', [{'LLM_REASONING_LOW_TOKENS': 5000},
    {'LLM_REASONING_HIGH_TOKENS': True}, {'LLM_REASONING_HIGH_TOKENS': 1048577},
    {'LLM_REASONING_LOW_TOKENS': 1}])
def test_invalid_budget_configuration_rejects_before_encoding(config):
    with pytest.raises(ProviderAccessError, match='budgets'):
        codec.encode(request(BUDGET), config=config)


@pytest.mark.parametrize('model', [CLAUDE, BUDGET, OPUS])
def test_claude_thinking_temperature_conflict_rejects_before_dispatch(model):
    with pytest.raises(ProviderAccessError, match='temperature'):
        codec.encode(request(model, temperature=.5))


@pytest.mark.parametrize('maximum', [1, 2047])
def test_insufficient_thinking_ceiling_is_not_silently_raised(maximum):
    with pytest.raises(ProviderAccessError, match='2048'):
        codec.encode(request(BUDGET, maximum=maximum))


@pytest.mark.parametrize('model', ['us.anthropic.claude-sonnet-99', 'us.openai.gpt-99-luna'])
def test_display_family_parsing_does_not_qualify_converse_effort(model):
    assert selection(model, 'converse', 'runtime').reasoning_efforts is None
    with pytest.raises((ProviderAccessError, ContractError, OperationalError)):
        codec.encode(request(model))


@pytest.mark.parametrize('value', [None, '', 'opaque', 'AA', 'AB==', '====', 'é', 'A' * 100000])
def test_opaque_encoding_must_be_bounded_canonical_base64(value):
    with pytest.raises(ContractError):
        state.binary(value)


def test_binary_encoding_never_changes_the_sdk_object_and_replays_exact_bytes():
    raw = response()
    opaque = bytes(range(256))
    raw['output']['message']['content'].insert(0, {'reasoningContent': {'redactedContent': opaque}})
    before = deepcopy(raw)
    wire = state.wire_response(raw, {'credential-sentinel'})
    result = codec.decode(wire, selected_llm=GPT)
    following = replace(request(), payload=CompletionRequest((result.payload, Message('user', (TextContent('Continue'),)))))
    body = codec.encode(following)
    restored = state.sdk_messages(body['messages'], {'credential-sentinel'})
    assert restored[0] == raw['output']['message']
    assert raw == before
    assert result.reasoning_summary is None
    assert result.payload.content == (TextContent('hello'),)
    assert base64.b64encode(opaque).decode() in result.payload.continuation
    from api_agent.api.chat_contract import result as browser_result
    assert 'redactedContent' not in json.dumps(browser_result('turn', result))
    from api_agent.workflows.capture import project_values
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    captured = project_values({'provider_response': wire}, {'type': 'object'},
        EmissionBoundary(ContentPolicy(True)), _provider_content=True)
    assert base64.b64encode(opaque).decode() not in str(captured)


@pytest.mark.parametrize('reasoning', [
    {'reasoningText': {'text': 'A brief explanation.', 'signature': 'opaque-signature'}},
    {'reasoningText': {'signature': 'opaque-signature'}},
    {'redactedContent': base64.b64encode(b'opaque-binary').decode()},
])
def test_native_tool_state_survives_tool_results_without_discard_or_reordering(reasoning):
    raw = response()
    raw['stopReason'] = 'tool_use'
    raw['output']['message']['content'] = [{'reasoningContent': reasoning},
        {'toolUse': {'toolUseId': 'call-1', 'name': 'lookup', 'input': {'query': 'value'}}}]
    answer = codec.decode(raw, selected_llm=GPT)
    following = replace(request(), payload=CompletionRequest((Message('user', (TextContent('Question'),)), answer.payload,
        Message('tool', results=(ToolResult('call-1', (TextContent('found'),)),))),
        tools=(Tool('lookup', 'Lookup', '{"type":"object"}', strict=False),)))
    body = codec.encode(following)
    assert body['messages'][1] == raw['output']['message']
    assert body['messages'][2]['content'][0]['toolResult']['toolUseId'] == 'call-1'
    assert answer.outcome == 'tool_calls'
    changed = replace(answer.payload, calls=(replace(answer.payload.calls[0], name='other'),))
    with pytest.raises(ProviderAccessError, match='continuation'):
        codec.continuation_message(changed, GPT)


@pytest.mark.parametrize('raw', [None, [], {'output': None}, {'output': {'message': None}},
    {'output': {'message': {'content': None}}}])
def test_non_binary_response_shapes_are_left_for_the_normal_decoder(raw):
    assert state.wire_response(raw, set()) == raw


@pytest.mark.parametrize('opaque', [b'', 'not-sdk-bytes', b'x' * 65537])
def test_sdk_binary_admission_rejects_invalid_shapes_and_size(opaque):
    raw = response()
    raw['output']['message']['content'].insert(0, {'reasoningContent': {'redactedContent': opaque}})
    with pytest.raises(ContractError):
        state.wire_response(raw, set())


def test_binary_state_cannot_smuggle_registered_credentials_in_either_direction():
    raw = response()
    raw['output']['message']['content'] = [{'reasoningContent': {'redactedContent': b'credential-sentinel'}}]
    with pytest.raises(ContractError, match='safely'):
        state.wire_response(raw, {'credential-sentinel'})
    messages = [{'content': [{'reasoningContent': {'redactedContent': base64.b64encode(b'credential-sentinel').decode()}}]}]
    with pytest.raises(ContractError, match='safely'):
        state.sdk_messages(messages, {'credential-sentinel'})


def test_omitted_ceiling_uses_shared_configuration_without_mutating_request():
    value = request(BUDGET, maximum=None)
    body = codec.encode(value, config={'LLM_MAX_OUTPUT_TOKENS': 5000})
    assert body['inferenceConfig']['maxTokens'] == 5000
    assert body['additionalModelRequestFields']['thinking']['budget_tokens'] == 3976
    assert value.payload.generation.max_output_tokens is None
    assert codec.encode(value)['inferenceConfig']['maxTokens'] == 64000


@pytest.mark.parametrize('model, expected', [(GPT, {'reasoning': {'effort': 'none'}}),
    (BUDGET, {'thinking': {'type': 'disabled'}}),
    ('us.anthropic.claude-opus-4-8', {'thinking': {'type': 'disabled'}}),
    ('us.anthropic.claude-haiku-5-5', {'thinking': {'type': 'disabled'}})])
def test_off_maps_only_to_qualified_native_disable(model, expected):
    assert 'off' in selection(model, 'converse', 'runtime').reasoning_efforts
    body = codec.encode(request(model, 'off', display=True))
    assert body['additionalModelRequestFields'] == expected
    controls.validate_fields(body, selection(model, 'converse', 'runtime'))


@pytest.mark.parametrize('model', [CLAUDE, 'us.anthropic.claude-opus-5-5', 'openai.gpt-oss-20b-1:0'])
def test_off_is_not_invented_for_unqualified_models(model):
    assert 'off' not in selection(model, 'converse', 'runtime').reasoning_efforts
    with pytest.raises(OperationalError):
        codec.encode(request(model, 'off'))


@pytest.mark.parametrize('model, value', [
    (GPT, []), (GPT, {'reasoning': {'effort': 'minimal'}}),
    (GPT, {'reasoning_effort': 'low'}), (GPT, {'reasoning': {'summary': 'private'}}),
    ('openai.gpt-oss-20b-1:0', {'reasoning': {'effort': 'low'}}),
    ('openai.gpt-oss-20b-1:0', {'reasoning_effort': 'none'}),
    (CLAUDE, {'thinking': {'type': 'disabled'}}),
    (CLAUDE, {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'unknown'}}),
    (CLAUDE, {'thinking': {'type': 'adaptive'}, 'output_config': []}),
    (BUDGET, {'thinking': {'type': 'enabled', 'budget_tokens': True}}),
    (BUDGET, {'thinking': {'type': 'enabled', 'budget_tokens': 1023}}),
    (BUDGET, {'thinking': {'type': 'enabled', 'budget_tokens': 8192}}),
    (OPUS, {'thinking': {'type': 'enabled', 'budget_tokens': 1024}}),
    (OPUS, {'thinking': {'type': 'enabled', 'budget_tokens': 1024},
        'output_config': {'effort': 'unknown'}, 'anthropic_beta': ['effort-2025-11-24']}),
    ('operator.model', {'thinking': {'type': 'adaptive'}}),
])
def test_worker_control_admission_rejects_before_authentication(model, value):
    from api_agent.llm_providers.bedrock import worker
    from test_bedrock_runtime_worker import request as worker_request
    frames = []
    body = {'modelId': model, 'messages': [], 'inferenceConfig': {'maxTokens': 8192},
            'additionalModelRequestFields': value}
    worker.execute(worker_request('converse', {'endpoint': 'runtime', 'prefix': '/converse', 'body': body},
        auth={'mode': 'session'}), frames.append,
        session_factory=lambda **kwargs: pytest.fail('Used credentials for invalid controls'))
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]


def test_frontier_converse_schema_requires_its_documented_strict_flag():
    from api_agent.providers.llm import StructuredOutput
    schema = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}'
    value = request()
    value = replace(value, payload=replace(value.payload, structured=StructuredOutput('answer', schema)))
    body = codec.encode(value)
    assert body['additionalModelRequestFields']['text'] == {'format': {'strict': True}}
    controls.validate_fields(body, selection(GPT, 'converse', 'runtime'))
    del body['additionalModelRequestFields']['text']
    with pytest.raises(ValueError, match='strict'):
        controls.validate_fields(body, selection(GPT, 'converse', 'runtime'))


def test_unsupported_temperature_is_rejected_from_catalog_and_worker():
    model = 'us.openai.gpt-6-astra'
    selected = selection(model, 'converse', 'runtime')
    assert 'temperature' not in selected.features
    with pytest.raises(OperationalError):
        codec.encode(request(model, temperature=1))
    with pytest.raises(ValueError, match='temperature'):
        controls.validate_fields({'inferenceConfig': {'temperature': 1}}, selected)


def test_budget_metadata_is_typed_and_retained_without_exporting_native_state():
    from api_agent.telemetry.inference_diagnostics import sdk_observation, valid_diagnostics
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    body = codec.encode(request(BUDGET, 'high'), config={'LLM_REASONING_HIGH_TOKENS': 20000})
    observed = sdk_observation({'provider_payload': body}, 'request')
    assert observed['thinking_budget_tokens'] == 7168
    assert observed['thinking_mode'] == 'enabled'
    assert observed['reasoning_control'] == 'budget'
    assert valid_diagnostics(observed)
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.content_export({'budget_tokens': 7168}) == {'budget_tokens': 7168}
    assert 'private' not in str(boundary.content_export({'budget_tokens': 'private'}))


def test_thinking_presets_visibility_comes_from_provider_declarations(tmp_path):
    from test_bedrock_configuration_review import declarations
    from test_tui import fixture
    fields = declarations()
    names = ['BEDROCK_API_MODE', *(f'LLM_REASONING_{name}_TOKENS' for name in ('LOW', 'MEDIUM', 'HIGH'))]
    app = fixture(tmp_path, fields={name: fields[name] for name in names})
    assert not any(name in app.session.result().fields for name in names[1:])
    app.session.set_value('BEDROCK_API_MODE', 'converse', persist=False)
    result = app.session.result()
    assert [result.values[name] for name in names[1:]] == [1024, 4096, 16384]


@pytest.mark.parametrize('configured', [True, 0, 1048577])
def test_invalid_shared_default_ceiling_is_rejected(configured):
    with pytest.raises(ProviderAccessError, match='ceiling'):
        codec.encode(request(BUDGET, maximum=None), config={'LLM_MAX_OUTPUT_TOKENS': configured})


@pytest.mark.parametrize('model', ['operator.model', CLAUDE])
def test_control_resolver_independently_rejects_unadmitted_effort(model):
    with pytest.raises(ProviderAccessError, match='no admitted'):
        controls.fields(request(model, 'off'))


def test_worker_strict_flag_is_not_a_general_extra_field_escape():
    with pytest.raises(ValueError, match='strict'):
        controls.validate_fields({'additionalModelRequestFields': {'text': {'format': {'strict': True}}}},
                                 selection(GPT, 'converse', 'runtime'))
    with pytest.raises(ValueError, match='strict'):
        controls.validate_fields({'outputConfig': {}, 'additionalModelRequestFields': {'text': {'format': {'strict': True}}}},
                                 selection(CLAUDE, 'converse', 'runtime'))


def test_worker_rechecks_claude_temperature_conflict():
    body = codec.encode(request(CLAUDE))
    body['inferenceConfig']['temperature'] = .5
    with pytest.raises(ValueError, match='temperature'):
        controls.validate_fields(body, selection(CLAUDE, 'converse', 'runtime'))


def test_replay_rejects_mixed_native_union_and_total_opaque_overflow():
    encoded = base64.b64encode(b'x' * 40000).decode()
    mixed = [{'content': [{'reasoningContent': {'redactedContent': encoded, 'reasoningText': {'text': 'no'}}}]}]
    with pytest.raises(ContractError, match='opaque state'):
        state.sdk_messages(mixed, set())
    excessive = [{'content': [{'reasoningContent': {'redactedContent': encoded}}] * 2}]
    with pytest.raises(ContractError, match='exceeds limit'):
        state.sdk_messages(excessive, set())


@pytest.mark.parametrize('additional, expected', [
    ({'reasoning': {'effort': 'high'}}, {'reasoning_effort': 'high', 'reasoning_control': 'native_effort'}),
    ({'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'}},
     {'reasoning_effort': 'low', 'thinking_mode': 'adaptive', 'reasoning_control': 'native_effort'}),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 1024}, 'output_config': {'effort': 'low'}},
     {'reasoning_effort': 'low', 'thinking_mode': 'enabled', 'thinking_budget_tokens': 1024,
      'reasoning_control': 'native_effort_and_budget'}),
    ({'thinking': {'type': [], 'budget_tokens': True}, 'output_config': []}, {}),
])
def test_native_request_observation_records_only_typed_controls(additional, expected):
    from api_agent.telemetry.inference_diagnostics import sdk_observation
    observed = sdk_observation({'provider_payload': {'additionalModelRequestFields': additional}}, 'request')
    assert observed == {'version': 1, 'observation': 'sdk_decoded', **expected}


@pytest.mark.parametrize('messages', [None, {}, [None], [{'content': None}]])
def test_replay_message_shapes_are_checked_before_sdk_conversion(messages):
    with pytest.raises(ContractError, match='message'):
        state.sdk_messages(messages, set())
