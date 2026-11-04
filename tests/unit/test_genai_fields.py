"""Source facts and native visible shapes are distinct admitted contracts."""
import json

import pytest

from api_agent.telemetry.trace_mapping import genai_messages
from api_agent.telemetry.tracing import _structural
from api_agent.telemetry.privacy import EmissionBoundary


@pytest.mark.parametrize('direction,value,expected', [
 ('input',{'provider_payload':{'instructions':'Be brief','input':[{'role':'user','content':[{'type':'input_text','text':'Question'}]}]}},
  [{'role':'system','parts':[{'type':'text','content':'Be brief'}]},{'role':'user','parts':[{'type':'text','content':'Question'}]}]),
 ('output',{'provider_response':{'output':[{'type':'reasoning','encrypted_content':'opaque'}, {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Answer'}]}]}},
  [{'role':'assistant','parts':[{'type':'text','content':'Answer'}]}]),
 ('input',{'provider_payload':{'system':[{'text':'Be brief'}],'messages':[{'role':'user','content':[{'text':'Question'}]}]}},
  [{'role':'system','parts':[{'type':'text','content':'Be brief'}]},{'role':'user','parts':[{'type':'text','content':'Question'}]}]),
 ('output',{'provider_response':{'output':{'message':{'role':'assistant','content':[{'text':'Answer'},{'reasoningContent':{'redactedContent':'opaque'}}]}}}},
  [{'role':'assistant','parts':[{'type':'text','content':'Answer'}]}]),
 ('output',{'provider_response':{'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'one','function':{'name':'lookup','arguments':'{}'}}]}}]}},
  [{'role':'assistant','parts':[{'type':'tool_call','id':'one','name':'lookup','arguments':'{}'}]}]),
 ('output',{'provider_response':{'output':[{'type':'function_call','call_id':'one','name':'lookup','arguments':'{}'}]}},
  [{'role':'assistant','parts':[{'type':'tool_call','id':'one','name':'lookup','arguments':'{}'}]}]),
 ('input',{'provider_payload':{'input':[{'type':'function_call_output','call_id':'one','output':'found'}]}},
  [{'role':'tool','parts':[{'type':'tool_call_response','id':'one','response':'found'}]}]),
 ('output',{'provider_response':{'output':{'message':{'role':'assistant','content':[{'toolUse':{'toolUseId':'one','name':'lookup','input':{}}}]}}}},
  [{'role':'assistant','parts':[{'type':'tool_call','id':'one','name':'lookup','arguments':{}}]}]),
])
def test_native_shapes_emit_only_documented_visible_message_parts(direction,value,expected):
    assert genai_messages(direction,value)==expected
    assert 'opaque' not in json.dumps(genai_messages(direction,value))


@pytest.mark.parametrize('name,expected', [('openai','openai'),('aws.bedrock','aws.bedrock'),
 ('independent.provider','independent.provider'),('https://example.com',None),
 ('synthetic-credential',None),('sk-'+'a'*48,None),('bearer.'+'a'*48,None),('private prompt text',None)])
def test_provider_identity_requires_declaration_shape_and_live_credential_admission(name,expected):
    boundary=EmissionBoundary(secrets=['synthetic-credential'])
    assert _structural(boundary,{'gen_ai.provider.name':name}).get('gen_ai.provider.name')==expected


@pytest.mark.parametrize('mode,base,expected', [('subscription','https://arbitrary.example/v1','openai'),
 ('api_key','https://api.openai.com/v1','openai'),('api_key','https://openrouter.ai/api/v1','openrouter'),
 ('api_key','https://arbitrary.example/v1',None),('api_key','https://api.openai.com/v1/alternate',None)])
def test_provider_factories_declare_only_exact_owned_public_routes(mode,base,expected):
    from api_agent.llm_providers.gpt.factory import GPTProvider
    provider=object.__new__(GPTProvider)
    provider.auth_mode,provider.base_url=mode,base
    assert provider.telemetry_provider_name==expected
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    assert BedrockProvider.telemetry_provider_name=='aws.bedrock'


@pytest.mark.parametrize('key', ['gen_ai.usage.cache_read.input_tokens',
 'gen_ai.usage.cache_write.input_tokens','gen_ai.usage.reasoning.output_tokens','gen_ai.request.max_tokens'])
@pytest.mark.parametrize('value', ['credential-token',True,-1,1.5,[],{},2**54,float('inf')])
def test_precise_count_aliases_never_admit_secret_or_invalid_values(key,value):
    from api_agent.telemetry.privacy import ContentPolicy
    for enabled in (False,True):
        boundary=EmissionBoundary(ContentPolicy(enabled))
        assert key not in _structural(boundary,{key:value})
        assert key not in boundary.export({key:value})


@pytest.mark.parametrize('key', ['gen_ai.usage.cache_read.input_tokens',
 'gen_ai.usage.cache_write.input_tokens','gen_ai.usage.reasoning.output_tokens','gen_ai.request.max_tokens'])
@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
def test_precise_count_aliases_remain_structural_under_all_flags(key,trace,log):
    from api_agent.telemetry.privacy import ContentPolicy
    boundary=EmissionBoundary(ContentPolicy(log))
    assert _structural(boundary.for_policy(ContentPolicy(trace)),{key:7})=={key:7}


@pytest.mark.parametrize('outcome', ['completed','length','refused'])
def test_generic_outcome_does_not_invent_native_finish_reason(outcome):
    from api_agent.telemetry.genai_fields import provider_fields
    from api_agent.providers.llm import LLMRequest,CompletionRequest,Message,TextContent,LLMResult,GenerationOptions
    request=LLMRequest('sample',CompletionRequest((Message('user',(TextContent('question'),)),),
        generation=GenerationOptions(max_output_tokens=200000)))
    result=LLMResult(Message('assistant',(TextContent('answer'),)),outcome=outcome)
    fields=provider_fields(request,result)
    assert fields['api_agent.llm.outcome']==outcome
    assert 'gen_ai.response.finish_reasons' not in fields
    assert 'gen_ai.request.max_tokens' not in fields
