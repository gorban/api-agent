"""Independent controls for native subset admission and no-network validation."""
from dataclasses import replace
import pytest
from referencing import Registry
from referencing.exceptions import NoSuchResource
from api_agent.llm_providers.gpt import rich
from api_agent.providers import rich_inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import GenerationOptions,LLMResult,Message,ToolCall,Usage
from test_rich_inference import request


@pytest.mark.parametrize('mode,subscription',[('responses',False),('responses',True),('completions',False)])
def test_native_rich_temperature_rejection_preserves_provider_unsupported(mode,subscription):
    original=request()
    original=replace(original,payload=replace(original.payload,generation=GenerationOptions(temperature=.5)))
    with pytest.raises(ProviderAccessError) as raised:
        rich.payload(original,mode,subscription)
    assert raised.value.kind == 'unsupported'


def test_native_sync_validation_checks_each_valid_tool_call_before_returning():
    original=request()
    calls=(ToolCall('one','lookup','{"value":"first"}'),ToolCall('two','lookup','{"value":"second"}'))
    result=LLMResult(Message('assistant',calls=calls),Usage(7,3),'tool_calls')
    assert rich.validate_result(original,result) is result
    invalid=replace(result,payload=Message('assistant',calls=(calls[0],replace(calls[1],arguments='{"value":3}'))))
    with pytest.raises(ProviderAccessError) as raised:
        rich.validate_result(original,invalid)
    assert raised.value.usage == Usage(7,3)


def test_configured_registry_retrieval_guard_refuses_external_resources():
    uri='https://example.invalid/schema.json'
    registry=Registry(retrieve=rich_inference._no_remote)
    with pytest.raises(NoSuchResource) as raised:
        registry.get_or_retrieve(uri)
    assert raised.value.ref == uri
