"""Reasoning preserves omission and explicit capability knowledge."""
from types import SimpleNamespace
import pytest
from api_agent.config import ConfigSchema, load_shared_schema, resolve_config
from api_agent.providers.cache_policy import configured_request, validate_provider_request
from api_agent.providers.catalog import catalog_from_wire, catalog_to_wire
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.inference import request_from_wire, request_to_wire
from api_agent.providers.llm import AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, Message, StructuredOutput, TextContent


def request(effort=None, rich=False):
    return LLMRequest('synthetic-model', CompletionRequest((Message('user', (TextContent('hello'),)),), generation=GenerationOptions(reasoning_effort=effort), structured=StructuredOutput('answer', '{"type":"object","properties":{},"required":[],"additionalProperties":false}') if rich else None), 'request-1')


@pytest.mark.parametrize('rich', [False, True])
@pytest.mark.parametrize('effort', [None, '', 'off', 'low', 'medium', 'high'])
def test_exact_optional_wire(rich, effort):
    baseline = request_to_wire(request(rich=rich))
    assert 'reasoning_effort' not in baseline
    actual = request_to_wire(request(effort, rich))
    assert actual == (baseline if effort is None else baseline | {'reasoning_effort': effort})
    assert request_from_wire(actual) == request(effort, rich)


@pytest.mark.parametrize('value', [False, 0, 1.0, [], {}, 'LOW', 'none', 'minimal'])
@pytest.mark.parametrize('rich', [False, True])
def test_invalid_effort(value, rich):
    with pytest.raises(ValueError):
        GenerationOptions(reasoning_effort=value)
    with pytest.raises(ContractError):
        request_from_wire(request_to_wire(request(rich=rich)) | {'reasoning_effort': value})


@pytest.mark.parametrize('efforts', [None, (), ('off', 'low', 'medium', 'high')])
def test_catalog_metadata(efforts):
    selection = AvailableSelection('synthetic-model', features=('reasoning_effort',), version='2026-01-01', family='synthetic', reasoning_efforts=efforts)
    row = catalog_to_wire((selection,))[0]
    assert ('reasoning_efforts' in row) is (efforts is not None)
    if efforts is not None:
        assert row['reasoning_efforts'] == list(efforts)
    assert catalog_from_wire([row]) == (selection,)
    assert (row['version'], row['family']) == ('2026-01-01', 'synthetic')


@pytest.mark.parametrize('efforts', [False, [], ('',), ('low', 'low'), ('extra',), (False,)])
def test_invalid_catalog_efforts(efforts):
    with pytest.raises(ValueError):
        AvailableSelection('synthetic-model', reasoning_efforts=efforts)


@pytest.mark.parametrize('efforts', [False, {}, [''], ['low', 'low'], ['extra'], [False]])
def test_invalid_catalog_wire(efforts):
    row = catalog_to_wire((AvailableSelection('synthetic-model'),))[0]
    with pytest.raises(ContractError):
        catalog_from_wire([row | {'reasoning_efforts': efforts}])


@pytest.mark.parametrize('effort', ['off', 'low', 'medium', 'high'])
def test_selection_requires_known_support(effort):
    for selection in (AvailableSelection('synthetic-model'), AvailableSelection('synthetic-model', features=('reasoning_effort',)), AvailableSelection('synthetic-model', features=('reasoning_effort',), reasoning_efforts=()), AvailableSelection('synthetic-model', reasoning_efforts=(effort,))):
        with pytest.raises(OperationalError):
            selection.validate(request(effort))
        selection.validate(request())
        selection.validate(request(''))
    AvailableSelection('synthetic-model', features=('reasoning_effort',), reasoning_efforts=(effort,)).validate(request(effort))


@pytest.mark.parametrize('explicit', [None, '', 'off', 'low', 'medium', 'high'])
def test_inheritance(explicit):
    original = request(explicit)
    actual = configured_request(original, 'medium', reasoning_effort='high')
    assert actual.payload.generation == GenerationOptions(cache_policy='medium', reasoning_effort='high' if explicit is None else explicit)
    assert original == request(explicit)
    assert configured_request(actual, 'off', reasoning_effort='low') is actual
    assert configured_request(original, '', reasoning_effort=None) is original


def test_helper_validation():
    assert configured_request('other', 'invalid', reasoning_effort='invalid') == 'other'
    adapter = SimpleNamespace()
    validate_provider_request(adapter, request(''))
    with pytest.raises(ProviderAccessError, match='LLM_REASONING_EFFORT'):
        validate_provider_request(adapter, request('off'))
    adapter.capabilities = SimpleNamespace(features=('reasoning_effort',))
    validate_provider_request(adapter, request('off'))


def test_config_precedence():
    schema = load_shared_schema()
    narrowed = ConfigSchema({'LLM_REASONING_EFFORT': schema.fields['LLM_REASONING_EFFORT']})
    def resolve(**kwargs):
        return resolve_config(narrowed, **kwargs).raise_for_errors().values['LLM_REASONING_EFFORT']
    assert resolve(environ={}) == ''
    assert resolve(dotenv={'LLM_REASONING_EFFORT': 'off'}, environ={}) == 'off'
    assert resolve(dotenv={'LLM_REASONING_EFFORT': 'off'}, environ={'LLM_REASONING_EFFORT': 'low'}) == 'low'
    assert resolve(environ={'LLM_REASONING_EFFORT': 'low'}, argv=['--llm-reasoning-effort=high']) == 'high'
    assert resolve(environ={'LLM_REASONING_EFFORT': 'high'}, argv=['--llm-reasoning-effort=']) == ''
