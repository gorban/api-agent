"""Node projections preserve exact inference histories and application schemas."""
import json
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import (AvailableSelection, CompletionRequest, GenerationOptions,
    LLMRequest, LLMResult, Message, ResponsePolicy, StructuredOutput, TextContent)
from api_agent.providers.presentation import reasoning_progress
from api_agent.workflows.inference_history import InferenceHistory
from api_agent.workflows.node_summary import SummaryServices, display_selection
from test_workflow_response_contract import admit, response_graph

SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'boolean'}},
          'required': ['answer'], 'additionalProperties': False}


def request(**changes):
    payload = CompletionRequest((Message('user', (TextContent('Check.'),)),),
        response=ResponsePolicy('structured', 'allow', StructuredOutput('check', json.dumps(SCHEMA))))
    return LLMRequest('selected', replace(payload, **changes), 'call')


class Provider:
    def __init__(self, text=None, summary=None):
        self.requests = []
        self.text = text if text is not None else json.dumps({'progress_summary': 'Checked the result.', 'result': {'answer': True}})
        self.summary = summary

    async def __call__(self, value):
        self.requests.append(value)
        return LLMResult(Message('assistant', (TextContent(self.text),)), reasoning_summary=self.summary)


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['auto', 'explanation'])
async def test_generated_projection_is_separate_from_original_replay_history(policy):
    provider, events = Provider(), []
    service = SummaryServices(InferenceHistory().services({'llm': provider}, 'check'), policy)['llm']
    async def capture(value):
        events.append(value)
    with reasoning_progress(capture):
        result = await service(request())
        await service(replace(request(), request_id='next'))
    assert json.loads(result.payload.content[0].text) == {'answer': True}
    assert result.reasoning_summary == 'Explanation: Checked the result.'
    assert result.response is None and result.payload.continuation is None
    assert [event.text for event in events] == [result.reasoning_summary] * 2
    prepared = provider.requests[0]
    schema = json.loads(prepared.payload.response.schema.schema)
    assert schema['properties']['result'] == SCHEMA
    assert prepared.payload.response.fallback == 'allow'
    assert provider.requests[1].payload.response == prepared.payload.response
    assert provider.requests[1].payload.messages[1].content[0].text == provider.text


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['auto', 'native'])
async def test_native_capability_uses_original_request_without_schema_changes(policy):
    provider = Provider(text='{"answer":true}', summary='Native summary.')
    expected = request()
    with display_selection(AvailableSelection('selected', features=('display_reasoning',))):
        result = await SummaryServices({'llm': provider}, policy)['llm'](expected)
    assert provider.requests == [replace(expected, payload=replace(expected.payload,
        generation=replace(expected.payload.generation, display_summary=True)))]
    assert result.reasoning_summary == 'Native summary.'


@pytest.mark.asyncio
@pytest.mark.parametrize('selection,generation', [
    (AvailableSelection('other', features=('display_reasoning',)), GenerationOptions()),
    (AvailableSelection('selected'), GenerationOptions()),
    (AvailableSelection('selected', features=('display_reasoning',)), GenerationOptions(reasoning_effort='off'))])
async def test_auto_does_not_infer_native_display_from_a_name_or_disabled_effort(selection, generation):
    provider = Provider()
    with display_selection(selection):
        await SummaryServices({'complete': provider}, 'auto')['complete'](request(generation=generation))
    assert 'progress_summary' in provider.requests[0].payload.response.schema.schema


@pytest.mark.asyncio
async def test_native_reasoning_wins_over_generated_explanation_without_duplicate_display():
    provider = Provider(summary='Native text.')
    result = await SummaryServices({'llm': provider}, 'explanation')['llm'](request())
    assert result.reasoning_summary == 'Native text.'


@pytest.mark.asyncio
async def test_missing_native_summary_does_not_trigger_an_extra_call_or_schema_change():
    provider = Provider(text='{"answer":true}')
    with display_selection(AvailableSelection('selected', features=('display_reasoning',))):
        result = await SummaryServices({'llm':provider},'auto')['llm'](request())
    assert result.reasoning_summary is None and len(provider.requests) == 1
    assert json.loads(provider.requests[0].payload.response.schema.schema) == SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, {}, {'progress_summary': '', 'result': {'answer': True}},
    {'progress_summary': '  ', 'result': {'answer': True}},
    {'progress_summary': 'x' * 4097, 'result': {'answer': True}},
    {'progress_summary': 'Good', 'result': {'answer': 'wrong type'}},
    {'progress_summary': 'Good', 'result': {'answer': True}, 'extra': True}])
async def test_generated_envelope_rejects_missing_oversized_or_wrongly_typed_values(value):
    provider = Provider(text=json.dumps(value))
    with pytest.raises(ContractError):
        await SummaryServices({'llm': provider}, 'explanation')['llm'](request())


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [None, ResponsePolicy('text', 'reject')])
async def test_generated_summary_without_json_contract_rejects_before_dispatch(response):
    provider = Provider()
    with pytest.raises(ContractError, match='JSON response schema'):
        await SummaryServices({'llm': provider}, 'auto')['llm'](request(response=response))
    assert provider.requests == []


@pytest.mark.asyncio
async def test_refusal_is_preserved_without_parsing_a_summary_envelope():
    expected = LLMResult(Message('assistant', (TextContent('Cannot comply.'),)), outcome='refused')
    async def provider(value):
        return expected
    result = await SummaryServices({'llm': provider}, 'explanation')['llm'](request())
    assert result is expected


def test_summary_services_borrows_all_other_services_and_mutations():
    original = {'plain': object(), 'llm': None}
    services = SummaryServices(original, 'native')
    assert services['plain'] is original['plain'] and services['llm'] is None
    services['next'] = 1
    assert dict(services) == original and len(services) == 3
    del services['next']
    assert 'next' not in original


@pytest.mark.asyncio
async def test_embedded_schema_rebases_references_without_editing_literal_data():
    original = {'type':'object', '$defs': {'flag': {'type':'boolean'}},
        'properties': {'answer': {'$ref':'#/$defs/flag'},
                       'literal': {'const': {'$ref':'#/literal-data'}}},
        'required':['answer','literal'], 'additionalProperties':False}
    value = {'answer':True, 'literal': {'$ref':'#/literal-data'}}
    provider = Provider(text=json.dumps({'progress_summary':'Checked.', 'result':value}))
    response = ResponsePolicy('structured', 'allow', StructuredOutput('refs', json.dumps(original), strict=False))
    result = await SummaryServices({'llm':provider}, 'explanation')['llm'](request(response=response))
    wrapped = json.loads(provider.requests[0].payload.response.schema.schema)
    assert wrapped['properties']['result']['properties']['answer']['$ref'] == '#/properties/result/$defs/flag'
    assert wrapped['properties']['result']['properties']['literal']['const'] == {'$ref':'#/literal-data'}
    assert json.loads(result.payload.content[0].text) == value


@pytest.mark.parametrize('policy', ['auto', 'native', 'explanation', '-'])
def test_declaration_captures_optional_policy(tmp_path, policy):
    entry = response_graph(tmp_path)
    node = tmp_path / 'workflows/inspect.md.html'
    text = node.read_text().replace('| Fallback |', '| Fallback | Display summary |')
    text = text.replace('| --- | --- | --- | --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- | --- |')
    node.write_text(text.replace('| structured | reject |', f'| structured | reject | {policy} |'))
    assert admit(entry).layout.tables['Functions'][0]['Display summary'] == policy


@pytest.mark.parametrize('policy,mode', [('unknown', 'structured'), ('auto', 'text'), ('explanation', 'text'), ('native', '-')])
def test_invalid_policy_or_missing_contract_rejects_declaration(tmp_path, policy, mode):
    from api_agent.workflows.layout import LayoutError
    entry = response_graph(tmp_path, mode=mode, fallback='-' if mode == '-' else 'reject')
    node = tmp_path / 'workflows/inspect.md.html'
    text = node.read_text().replace('| Fallback |', '| Fallback | Display summary |')
    text = text.replace('| --- | --- | --- | --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- | --- |')
    suffix = f'| {mode} | ' + ('-' if mode == '-' else 'reject') + ' |'
    text = text.replace(suffix, suffix + f' {policy} |')
    if mode in ('text', '-'):
        text = text.replace('[response](../schemas/response.json)', '-')
    node.write_text(text)
    with pytest.raises(LayoutError):
        admit(entry)


@pytest.mark.asyncio
async def test_workflow_execution_installs_the_declared_summary_policy(tmp_path):
    from api_agent.workflows.definition import load_native_catalog
    from api_agent.workflows.execution import Workflow, NodeFunction
    entry = response_graph(tmp_path)
    native = tmp_path / 'native.py'
    native.write_text('''from api_agent.providers.llm import LLMRequest,Message,TextContent
async def inspect(inputs, context):
    request = LLMRequest('selected',context.response_request((Message('user',(TextContent('Check.'),)),)))
    await context.services['llm'](request)
    return {'seen':True}
''')
    node = tmp_path / 'workflows/inspect.md.html'
    text = node.read_text().replace('| Fallback |', '| Fallback | Display summary |')
    text = text.replace('| --- | --- | --- | --- | --- | --- | --- |', '| --- | --- | --- | --- | --- | --- | --- | --- |')
    node.write_text(text.replace('| structured | reject |', '| structured | reject | native |'))
    definition = admit(entry)
    function = load_native_catalog(definition, native)['inspect']
    provider = Provider(text='{"answer":true}')
    result = await Workflow(definition, {'inspect':NodeFunction(function,'llm')}, services={'llm':provider}).arun({})
    assert result['inspect'] == {'seen':True}
    assert provider.requests[0].payload.generation.display_summary is True


def test_legacy_layout_parser_accepts_optional_summary_column(tmp_path):
    from api_agent.workflows.layout import load_layout
    entry = response_graph(tmp_path)
    definition = admit(entry)
    # V1 and V2 share response semantics; this exercises V1's own table parser.
    layout = tmp_path / 'layout.md'
    layout.write_text('''# LangGraph Layout v1

## Flow

1.  [node inspect](native.py)

## Components

## Functions

| ID | Registry | Input | Output | Response schema | Output mode | Fallback | Display summary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| inspect | inspect | [input](schemas/input.json) | [output](schemas/output.json) | [response](schemas/response.json) | structured | allow | auto |

## Bindings

## Edges

## Prompts

## Timeouts
''')
    assert load_layout(layout, base_root=tmp_path, pack_root=tmp_path).tables['Functions'][0]['Display summary'] == 'auto'
