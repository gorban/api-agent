"""Public pricing and immutable, content-free historical capture fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import httpx
import pytest

from api_agent.providers import rates
from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import Usage, LLMResult, Message, TextContent
from api_agent.providers.inference import result_from_wire, result_to_wire
from api_agent.telemetry import accounting
from api_agent.telemetry.usage import SuiteUsage
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult


def _pricing_config(**values):
    from api_agent.llm_providers.gpt import factory
    return {'LLM_PROVIDER': str(Path(factory.__file__).resolve()) + ':factory', **values}


def _html(rows, tier='standard'):
    from html import escape
    props = dict(tier=[0, tier], rows=[1, [[1, [[0, value] for value in row]] for row in rows]])
    return ('<astro-island component-export="TextTokenPricingTables" props="' + escape(json.dumps(props), quote=True) + '"></astro-island>').encode()


def _evidence(**kw):
    return dict(call_id='00000000-0000-4000-8000-000000000001', selection='gpt-6-luna', actual_model=None, service_tier=None, serving_revision=None, usage=None, quote=None,
                started_at=None, finished_at=None, attempt=1, turn_id='00000000-0000-4000-8000-000000000001', turn_kind='initial', outcome='unknown', **kw)


def test_exact_categories_and_wire():
    card = rates.bundled_rate_card()
    assert rates.price_usage(card, 'gpt-6-luna', Usage(1000000, 1000000, 100000, cache_write_tokens=0)) == pytest.approx(.932)
    assert rates.price_usage(card, 'gpt-6-luna', Usage(0, 0, 0, cache_write_tokens=0)) == 0
    assert rates.price_usage(card, 'gpt-6-luna', Usage(1, 1)) is None
    assert rates.price_usage(card, 'alias', Usage(1, 1, 0, cache_write_tokens=0)) is None
    assert rates.price_usage(card, 'gpt-6-luna', Usage()) is None
    card['models']['gpt-6-luna']['cached_input'] = None
    assert rates.price_usage(card, 'gpt-6-luna', Usage(1, 1, 1, cache_write_tokens=0)) is None
    assert rates.price_usage(card, 'gpt-6-luna', Usage(1, 1, 0, cache_write_tokens=0)) == pytest.approx(.0000006)
    with pytest.raises(ContractError):
        rates.price_usage(card, 'x', {'input_tokens': True})
    result = LLMResult(Message('assistant', (TextContent('ok'),)), actual_model='gpt-exact')
    assert result_from_wire(result_to_wire(result)).actual_model == 'gpt-exact'


@pytest.mark.parametrize('change', [lambda c: c.update(version=True), lambda c: c.update(source='x'),
    lambda c: c.update(verified_at='today'), lambda c: c.update(verified_at='2026-01-01'),
    lambda c: c.update(models={}), lambda c: c['models']['gpt-6-luna'].update(input=-1),
    lambda c: c['models']['gpt-6-luna'].update(input=True), lambda c: c['models']['gpt-6-luna'].update(threshold=0),
    lambda c: c['models']['gpt-6-luna'].update(input_multiplier=.5)])
def test_invalid_card(change):
    card = rates.bundled_rate_card()
    change(card)
    with pytest.raises(ContractError):
        rates.validate_rate_card(card)


def test_parsers():
    raw = _html([['gpt-test', 1, .1, 2], ['gpt-new', 1, '-', 1.25, 2], ['gpt-context (<272K)', 1, 1, 1], ['audio', 1, 1, 1]])
    card = rates.parse_openai(raw)
    assert set(card['models']) == {'gpt-test', 'gpt-new'}
    assert card['models']['gpt-new']['cached_input'] is None
    assert card['models']['gpt-new']['cache_write'] == .00000125
    with pytest.raises(ContractError):
        rates.parse_openai(_html([['gpt-test', 1, 1, 2], ['gpt-test', 2, 2, 2]]))
    with pytest.raises(ContractError):
        rates.parse_openai(_html([['gpt-test', 1, 1, 2]], 'fast'))
    router = {'data': [{'id': 'openai/gpt-test', 'pricing': {'prompt': '.0001', 'completion': '.0002'}}, {'id': 'image', 'pricing': {'image': '2'}}]}
    assert rates.parse_openrouter(json.dumps(router).encode())['models']['openai/gpt-test']['input'] == .0001
    router['data'].append(router['data'][0])
    with pytest.raises(ContractError):
        rates.parse_openrouter(json.dumps(router).encode())


def test_refresh_cache_and_failure(tmp_path, monkeypatch):
    context = SimpleNamespace(owner_root=tmp_path)
    responses = [httpx.Response(200, content=_html([['gpt-test', 1, 1, 2]]), headers={'etag': 'v1'}), httpx.Response(304), httpx.Response(503)]
    seen = []
    def handler(request):
        seen.append(request)
        return responses.pop(0)
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    card = rates.freeze_pricing(context, _pricing_config())
    assert card['fallback'] is False
    assert rates.cached_rate_card(context, _pricing_config()) == card
    assert rates.freeze_pricing(context, _pricing_config()) == card
    assert seen[1].headers['If-None-Match'] == 'v1'
    assert rates.freeze_pricing(context, _pricing_config()) == card
    assert not any('authorization' in request.headers for request in seen)
    assert rates.freeze_pricing(context, {'LLM_PROVIDER': '/custom/factory.py:factory'}) is None
    assert rates.cached_rate_card(context, _pricing_config(GPT_AUTH_MODE='api_key', GPT_BASE_URL='https://custom.invalid/v1')) is None
    assert rates.cached_rate_card(context, _pricing_config(GPT_AUTH_MODE='api_key', GPT_BASE_URL='https://openrouter.ai/api/v1')) is None


def test_accounting_source_calls_and_immutability(tmp_path):
    source = tmp_path / 'test.py'
    source.write_text('test')
    test = TestIdentity('repo::test.py::test[param]', 'repo', 'ignored', 'test.py::test[param]', root=tmp_path)
    assert accounting.source_fingerprint(test) == accounting.sha256(b'test').hexdigest()
    assert accounting.source_fingerprint(TestIdentity('a', 'r', '', 'missing', root=tmp_path)) is None
    assert accounting.source_fingerprint(TestIdentity('a', 'r', '', '../outside', root=tmp_path)) is None
    assert accounting.source_fingerprint(TestIdentity('a', 'r', '', 'test.py')) is None
    (tmp_path / 'link').symlink_to(source)
    assert accounting.source_fingerprint(TestIdentity('a', 'r', '', 'link', root=tmp_path)) is None
    usage = SuiteUsage('run')
    evidence = _evidence()
    usage.record_inference('00000000-0000-4000-8000-000000000001', test.id, evidence)
    artifact = accounting.build_accounting(SimpleNamespace(run_id='00000000-0000-4000-8000-000000000002'), [test], {}, usage, [TestResult(test, 'passed')])
    assert artifact['tests'][0]['attempts'] == 1
    assert artifact['tests'][0]['calls'][0]['usage'] is None
    evidence['selection'] = 'changed'
    assert artifact['tests'][0]['calls'][0]['selection'] == 'gpt-6-luna'
    bad = deepcopy(artifact)
    bad['tests'][0]['calls'].append(deepcopy(bad['tests'][0]['calls'][0]))
    with pytest.raises(ContractError):
        accounting.validate_accounting(bad)


@pytest.mark.parametrize('change', [lambda v: v.update(version=2), lambda v: v.update(actual_model=2),
 lambda v: v.update(compatibility={}), lambda v: v.update(extra='payload'), lambda v: v.update(rate_card={})])
def test_metadata_validation(change):
    value = dict(version=1, requested_model='x', catalog_model='x', actual_model=None, serving_revision=None,
                 service_tier='standard', billing_mode='api_key', rate_card=None, compatibility=accounting.compatibility({}))
    assert accounting.validate_metadata(value) is value
    change(value)
    with pytest.raises(ContractError):
        accounting.validate_metadata(value)


@pytest.mark.parametrize('key,value', [('attempt', True), ('attempt', 0), ('turn_id', ''), ('outcome', 'bad'), ('started_at', 'today'), ('started_at', '2026-01-01'), ('actual_model', 1), ('usage', {})])
def test_evidence_rejections(key, value):
    evidence = _evidence()
    evidence[key] = value
    with pytest.raises((ContractError, ValueError)):
        accounting.validate_evidence(evidence)


def test_cache_rejects_untrusted_files(tmp_path):
    assert rates._read_cache(None) is None
    assert rates._read_cache(tmp_path) is None
    path = tmp_path / 'cache'
    path.write_bytes(b'x' * 2097153)
    assert rates._read_cache(path) is None
    path.write_text('{}')
    assert rates._read_cache(path) is None
    path.write_text(json.dumps(dict(card=rates.bundled_rate_card(), etag='\r', last_modified=None)))
    assert rates._read_cache(path) is None
    good = rates.bundled_rate_card()
    assert rates.cached_rate_card(config=_pricing_config(_COST_RATE_CARD=good)) == good
    assert rates.cached_rate_card(config=_pricing_config(_COST_RATE_CARD={}))['fallback']
    good['source'] = rates.OPENROUTER
    assert rates.cached_rate_card(config=_pricing_config(_COST_RATE_CARD=good))['source'] == rates.OPENAI


@pytest.mark.parametrize('kind', ['encoding', 'limit', 'timeout', 'persist'])
def test_refresh_bounds(tmp_path, monkeypatch, kind):
    original = httpx.Client
    raw = b'x' * 2097153 if kind == 'limit' else _html([['gpt-test', 1, 1, 2]])
    response = httpx.Response(200, content=raw, headers={'content-encoding': 'br'} if kind == 'encoding' else {})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(lambda req: response), **kw))
    if kind == 'timeout':
        times = iter([0, 6] + [6]*200)
        monkeypatch.setattr(rates.time, 'monotonic', lambda: next(times))
    if kind == 'persist':
        (tmp_path / '.api-agent').write_text('blocked')
    card = rates.freeze_pricing(SimpleNamespace(owner_root=tmp_path), _pricing_config())
    assert card['fallback'] is (kind != 'persist')


def _artifact(tmp_path):
    test = TestIdentity('r::t', 'r', 't', 't', root=tmp_path)
    usage = SuiteUsage('run')
    usage.record_inference('00000000-0000-4000-8000-000000000001', test.id, _evidence())
    return accounting.build_accounting(SimpleNamespace(run_id='00000000-0000-4000-8000-000000000002'), [test], {}, usage, [TestResult(test, 'passed')])


@pytest.mark.parametrize('mutate', [lambda a: a.update(version=0), lambda a: a.update(rate_card={}),
 lambda a: a['quota'].update(pct_point_delta=101), lambda a: a['quota'].update(before={}),
 lambda a: a['quota'].update(before=dict(allocation_id=None, observation_id=None, pct_used=101, window_id=None, reset_at=None,captured_at='2026-01-01T00:00:00+00:00')),
 lambda a: a['quota'].update(before=dict(allocation_id=None, observation_id=None, pct_used=1, window_id=None, reset_at=None,captured_at='2026-01-01T00:00:00+00:00',pct_step=0)),
 lambda a: a['tests'][0].update(source_fingerprint='0'*63), lambda a: a['tests'][0].update(source_fingerprint='g'*64),
 lambda a: a['tests'][0].update(source_fingerprint=True), lambda a: a['tests'][0].update(outcomes=['invalid']),
 lambda a: a['tests'][0].update(original_estimate=-1), lambda a: a['tests'][0].update(attempts=2),
 lambda a: a['tests'][0]['calls'][0].update(test_id='wrong'), lambda a: a['tests'][0].update(prediction=float('nan'))])
def test_artifact_rejections(tmp_path, mutate):
    value = _artifact(tmp_path)
    mutate(value)
    with pytest.raises(ContractError):
        accounting.validate_accounting(value)


def test_artifact_estimates_quota_and_metadata(tmp_path, monkeypatch):
    value = _artifact(tmp_path)
    value['rate_card'] = rates.bundled_rate_card()
    value['quota']['before'] = dict(allocation_id=None, observation_id=None, pct_used=None, window_id=None, reset_at=None,captured_at='2026-01-01T00:00:00+00:00',pct_step=1)
    value['quota']['after'] = dict(allocation_id=None, observation_id=None, pct_used=1, window_id=None, reset_at=None,captured_at='2026-01-01T00:00:00+00:00')
    value['quota']['payment_policy'] = dict(usd=100, quota_seconds=604800, period='quota')
    assert accounting.validate_accounting(value) is value
    evidence = _evidence()
    evidence.update(started_at='2026-01-01T00:00:00+00:00', finished_at='2025-01-01T00:00:00+00:00')
    with pytest.raises(ContractError):
        accounting.validate_evidence(evidence)
    metadata = dict(version=1, requested_model='x', catalog_model='x', actual_model='x', serving_revision=None,service_tier='standard',billing_mode='api_key',rate_card=rates.bundled_rate_card(),compatibility=accounting.compatibility({}))
    assert accounting.validate_metadata(metadata) is metadata
    metadata['compatibility']['LLM_DEFAULT_MODEL'] = {}
    with pytest.raises(ContractError):
        accounting.validate_metadata(metadata)
    metadata['compatibility']['LLM_DEFAULT_MODEL'] = float('nan')
    with pytest.raises(ContractError):
        accounting.validate_metadata(metadata)
    monkeypatch.setattr(accounting.json, 'dumps', lambda *a, **kw: 'x'*2097153)
    metadata['compatibility']['LLM_DEFAULT_MODEL'] = None
    with pytest.raises(ContractError):
        accounting.validate_metadata(metadata)
    monkeypatch.undo()
    folder = tmp_path / 'folder'
    folder.mkdir()
    assert accounting.source_fingerprint(TestIdentity('a','r','','folder', root=tmp_path)) is None
    source = tmp_path / 'large'
    source.write_bytes(b'x'*4194305)
    assert accounting.source_fingerprint(TestIdentity('a','r','','large', root=tmp_path)) is None


def test_nonregular_files_duplicates_and_no_context(tmp_path, monkeypatch):
    import os
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    assert rates._read_cache(fifo) is None
    assert accounting.source_fingerprint(TestIdentity('a','r','','fifo',root=tmp_path)) is None
    value = _artifact(tmp_path)
    value['tests'][0]['calls'].append(deepcopy(value['tests'][0]['calls'][0]))
    value['tests'][0]['observed_call_count'] = 2
    with pytest.raises(ContractError, match='Duplicate accounting call'):
        accounting.validate_accounting(value)
    assert rates.parse_openai(b'<div>ignore</div>' + _html([['gpt-test', 1, 1, 1]]))
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(lambda req: httpx.Response(200,content=_html([['gpt-test',1,1,1]]))), **kw))
    assert rates.freeze_pricing(None, _pricing_config())['fallback'] is False


def test_uuid_times_and_context_metadata(tmp_path):
    assert not accounting._uuid('invalid')
    assert not accounting._timestamp('invalid')
    assert not accounting._timestamp(None)
    value = _artifact(tmp_path)
    value['quota']['before'] = dict(allocation_id=None, observation_id=None, pct_used=1, window_id=None, reset_at=None,captured_at=None)
    with pytest.raises(ContractError, match='quota identity/time'):
        accounting.validate_accounting(value)
    metadata = dict(version=1, requested_model='x',catalog_model='x',actual_model=None,serving_revision=None,service_tier='standard',billing_mode='api_key',rate_card=None,compatibility=accounting.compatibility({}))
    metadata['compatibility']['_COST_REQUEST_SETTINGS'] = {'temperature': '0'*64}
    metadata['compatibility']['_COST_PACK_IDENTITY'] = '1'*64
    assert accounting.validate_metadata(metadata) is metadata
    metadata['compatibility']['_COST_REQUEST_SETTINGS'] = {'x': 'bad'}
    with pytest.raises(ContractError, match='request settings'):
        accounting.validate_metadata(metadata)
    metadata['compatibility']['_COST_REQUEST_SETTINGS'] = {}
    metadata['compatibility']['_COST_PACK_IDENTITY'] = 'bad'
    with pytest.raises(ContractError, match='pack'):
        accounting.validate_metadata(metadata)


def test_compatibility_shape():
    with pytest.raises(ContractError, match='compatibility shape'):
        accounting.validate_compatibility({})


def test_quote_frozen_snapshot_optional_metadata_and_actual(tmp_path, monkeypatch):
    from api_agent.llm_providers.gpt.pricing import quote
    from api_agent.providers.costs import validate_quote
    from api_agent.providers.llm import LLMRequest, CompletionRequest
    from api_agent.testing.inference_usage import publish, capture
    request = LLMRequest('gpt-6-luna', CompletionRequest((Message('user',(TextContent('secret text'),)),)))
    result = LLMResult(Message('assistant',(TextContent('answer'),)),Usage(10,2,0, cache_write_tokens=0),actual_model='unpriced-revision')
    quoted = quote(request,result,direct=True,subscription=False)
    assert quoted['list_cost_usd'] is None
    assert validate_quote(quoted) == quoted
    assert quote(request,result,direct=False,subscription=False)['accounting']['rate_card'] is None
    card = rates.bundled_rate_card()
    result = LLMResult(result.payload,result.usage,actual_model='gpt-6-luna')
    config = accounting.compatibility({})
    quoted = quote(request,result,direct=True,subscription=True,rate_card=card,compatibility=config)
    assert quoted['list_cost_usd'] == pytest.approx(.000002)
    assert quote(request,result,direct=False,subscription=False,rate_card=card)['list_cost_usd'] is None
    card['source'] = rates.OPENROUTER
    assert quote(request,result,direct=False,subscription=False,rate_card=card)['list_cost_usd'] == pytest.approx(.000002)
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test (call)')
    call_id = publish(request)
    publish(request,result,call_id=call_id,quote=quoted)
    tracker = SuiteUsage('run')
    for line in path.read_text().splitlines():
        data = json.loads(line)['data']
        event = SimpleNamespace(text=json.dumps(data),identity=SimpleNamespace(id='test'))
        capture(event,tracker)
        capture(event,tracker)
    assert tracker.rows()[0]['llm_calls'] == 1
    assert tracker.inference_calls[call_id]['actual_model'] == 'gpt-6-luna'
    assert 'secret text' not in path.read_text() and 'answer' not in path.read_text()


@pytest.mark.parametrize('model', [True, '', 'x'*201, 'token', '\n'])
def test_inference_model_identity_scrubs_token(model):
    from api_agent.llm_providers.gpt.inference import _result
    from api_agent.providers.contracts import ProviderAccessError
    data = dict(status='completed',output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='ok')])],model=model)
    with pytest.raises(ProviderAccessError,match='model identity'):
        _result(data,True,'token')


def test_cache_policy_boundary_branches():
    from api_agent.providers.llm import GenerationOptions
    from api_agent.providers.inference import request_from_wire
    assert GenerationOptions(cache_policy='').cache_policy is None
    with pytest.raises(ContractError):
        request_from_wire(dict(selected_llm='gpt-test',messages=[dict(role='user',text='x')],max_output_tokens=1,cache_policy='invalid',request_id=''))


def _grouped(row=None, *, threshold=True, groups=None):
    from html import escape
    def pack(v):
        if type(v) is list:
            return [1, [pack(item) for item in v]]
        if type(v) is dict:
            return [0, {key: pack(item) for key, item in v.items()}]
        return [0,v]
    headings = ['Model', 'Short context input', 'Short context cached input', 'Short context cache writes', 'Short context output', 'Long context input', 'Long context cached input', 'Long context cache writes', 'Long context output']
    props = {'headings':headings,'headingGroups':[dict(tooltip='≤272K input tokens'),dict(tooltip='>272K input tokens')] if threshold else [],'groups':groups or [dict(model='gpt-5.6-sol',rows=[row or [4,.4,5,20,8,.8,10,30]])]}
    return ('<astro-island component-export="GroupedPricingTable" props="'+escape(json.dumps({k:pack(v) for k,v in props.items()}),quote=True)+'"></astro-island>').encode()


def test_official_context_threshold_does_not_infer_family():
    raw = _html([['gpt-5.6-sol',4,.4,5,20],['gpt-other (<272K context length)',1,.1,2]]) + _grouped()
    card = rates.parse_openai(raw)
    rate = card['models']['gpt-5.6-sol']
    assert rate['threshold']==272000
    assert rates.price_usage(card,'gpt-5.6-sol',Usage(272000,100,0, cache_write_tokens=0)) == pytest.approx(1.09)
    assert rates.price_usage(card,'gpt-5.6-sol',Usage(300000,100,0, cache_write_tokens=0)) == pytest.approx(2.403)
    assert rates.price_usage(card,'gpt-other',Usage(1,1,0, cache_write_tokens=0)) is None
    assert rates.parse_openai(_grouped(threshold=False))['models']['gpt-5.6-sol']['input'] is None
    assert rates.parse_openai(_grouped([4,.4,5,20,8,.9,10,30]))['models']['gpt-5.6-sol']['input'] is None
    assert rates.parse_openai(_grouped([4,.4,5,20,'-','-',10,'-']))['models']['gpt-5.6-sol']['input'] is None
    raw = _html([['gpt-new',1,.1,2]]).replace(b'</astro-island>',b'<span>Long context</span></astro-island>')
    assert rates.parse_openai(raw)['models']['gpt-new']['input'] is None
    with pytest.raises(ContractError,match='Conflicting context'):
        rates.parse_openai(_grouped() + _grouped([5,.5,5,20,10,1,10,30]))
    with pytest.raises(ContractError,match='context pricing rows'):
        rates.parse_openai(_grouped(groups=[dict(model='x',rows=[[1,2]])]))


@pytest.mark.parametrize('packed', [None, [], [2,1], [0,1,2], [1,3]])
def test_packed_pricing_rejects_invalid(packed):
    with pytest.raises(ContractError):
        rates._unpack(packed)


def test_unpacked_undefined_and_nonprice():
    assert rates._unpack([0]) is None
    with pytest.raises(ContractError):
        rates._usd(True)
    # Other grouped categories do not become text Standard rates.
    assert rates.parse_openai(_html([['gpt-x',1,.1,2]]) + b'<astro-island component-export="GroupedPricingTable" props="{&quot;headings&quot;:[1,[]]}"></astro-island>')


@pytest.mark.parametrize('data', [None,3,{}, {'data':3},{'data':[3]},{'data':[{'id':'x','pricing':3}]}])
def test_router_scalars_rejected(data):
    with pytest.raises(ContractError):
        rates.parse_openrouter(json.dumps(data).encode())


def test_empty_packed_row_refresh_fails_soft(tmp_path,monkeypatch):
    original = httpx.Client
    content = b'<astro-island component-export="TextTokenPricingTables" props="{&quot;tier&quot;:[0,&quot;standard&quot;],&quot;rows&quot;:[1,[[1,[]]]]}"></astro-island>'
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=content)),**kw))
    assert rates.freeze_pricing(SimpleNamespace(owner_root=tmp_path),_pricing_config())['fallback']
    path=tmp_path/'nested'
    path.write_text('['*1500 + ']'*1500)
    assert rates._read_cache(path) is None


def test_large_card_compaction_keeps_selected_and_call_small():
    card = rates.bundled_rate_card()
    rate = card['models']['gpt-6-sol']
    card['models'] = {f'provider/model-{i:05d}':deepcopy(rate) for i in range(6000)}
    compact = rates.compact_rate_card(card,dict(LLM_DEFAULT_MODEL='provider/model-05999',LLM_ALLOWED_MODELS='provider/model-0599[0-9]'))
    assert len(json.dumps(compact).encode()) <= 262144
    assert 'provider/model-05999' in compact['models']
    assert compact['revision']==card['revision'] and compact['verified_at']==card['verified_at']
    missing = next(model for model in card['models'] if model not in compact['models'])
    assert rates.price_usage(compact,missing,Usage(1,1,0, cache_write_tokens=0)) is None
    one = rates.compact_rate_card(compact,model=missing)
    assert len(one['models'])==1 and rates.price_usage(one,missing,Usage(1,1,0, cache_write_tokens=0)) is None
    assert rates.price_usage(one,missing,Usage(0,0,0, cache_write_tokens=0)) is None
    assert len(json.dumps(one).encode()) < 1000
    assert rates.compact_rate_card(None) is None
    assert rates.compact_rate_card(card,{'LLM_DEFAULT_MODEL':'('})


def test_compact_selection_budget(monkeypatch):
    values=iter([0,1])
    monkeypatch.setattr(rates.time,'monotonic',lambda:next(values))
    assert rates.compact_rate_card(rates.bundled_rate_card())


def test_response_billing_wire_and_unknown_tier():
    from api_agent.llm_providers.gpt.inference import _result
    from api_agent.llm_providers.gpt.pricing import quote
    from api_agent.providers.llm import LLMRequest,CompletionRequest
    from api_agent.providers.contracts import ProviderAccessError
    data=dict(status='completed',output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='ok')])],model='gpt-6-luna',service_tier='flex',serving_revision='explicit-revision',usage=dict(input_tokens=10,output_tokens=1,input_tokens_details=dict(cached_tokens=0)))
    result=_result(data,True,'token')
    assert result_from_wire(result_to_wire(result)) == result
    request=LLMRequest('gpt-6-luna',CompletionRequest((Message('user',(TextContent('x'),)),)))
    quoted=quote(request,result,direct=True,subscription=False)
    assert quoted['list_cost_usd'] is None
    assert quoted['accounting']['service_tier']=='flex'
    assert quoted['accounting']['serving_revision']=='explicit-revision'
    assert quoted['accounting']['billing_mode']=='api_key'
    for field in ('service_tier','serving_revision'):
        bad={**data,field:'token'}
        with pytest.raises(ProviderAccessError,match='billing metadata'):
            _result(bad,True,'token')


def test_multiple_calls_same_user_turn_and_failed_terminal(tmp_path,monkeypatch):
    from api_agent.testing.inference_usage import publish,capture,user_turn,_STARTS
    from api_agent.providers.llm import LLMRequest,CompletionRequest
    request=LLMRequest('gpt-6-luna',CompletionRequest((Message('user',(TextContent('x'),)),)))
    result=LLMResult(Message('assistant',(TextContent('x'),)),Usage(1,1,0, cache_write_tokens=0))
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test (call)')
    turn='00000000-0000-4000-8000-000000000012'
    with user_turn(turn,'clarification'):
        first=publish(request,attempt=2)
        publish(request,result,call_id=first,attempt=2)
        second=publish(request,attempt=2)
        publish(request,result,call_id=second,attempt=2)
        failed=publish(request,attempt=2)
        publish(request,call_id=failed,failed=True,attempt=2)
    assert first not in _STARTS and failed not in _STARTS
    tracker=SuiteUsage('run')
    for line in path.read_text().splitlines():
        row=json.loads(line)['data']
        capture(SimpleNamespace(text=json.dumps(row),identity=SimpleNamespace(id='test')),tracker)
    assert tracker.report()['prompt_count']==1
    assert tracker.report()['prompt_counts']['clarification']==1
    assert tracker.rows()[0]['llm_calls']==2
    assert tracker.inference_calls[failed]['outcome']=='failed'
    assert tracker.inference_calls[failed]['finished_at'] is not None
    with pytest.raises(ValueError):
        with user_turn(turn,'invalid'):
            pass


def test_nonselected_usage_is_not_complete_zero(tmp_path):
    test=TestIdentity('selected','r','test.py','test.py',root=tmp_path)
    usage=SuiteUsage('run')
    usage.add_usage(provider='legacy',model='m',session_id='unassigned',input_tokens=1,output_tokens=1)
    artifact=accounting.build_accounting(SimpleNamespace(run_id='00000000-0000-4000-8000-000000000001'),[test],{},usage,[],estimator=SimpleNamespace(predictions={'selected':dict(estimated_cost_usd=2)},advance_predictions={'selected':dict(estimated_cost_usd=1)}))
    assert artifact['tests'][0]['calls_complete'] is False
    assert artifact['tests'][0]['prediction']['estimated_cost_usd']==1
    usage=SuiteUsage('run')
    evidence=_evidence()
    usage.record_inference(evidence['call_id'],'unassigned',evidence)
    artifact=accounting.build_accounting(SimpleNamespace(run_id='00000000-0000-4000-8000-000000000001'),[test],{},usage,[])
    assert artifact['tests'][0]['calls_complete'] is False


def test_compact_none_model_names():
    card=rates.bundled_rate_card()
    card['models']={str(i):deepcopy(card['models']['gpt-6-sol']) for i in range(2000)}
    compact=rates.compact_rate_card(card)
    assert len(compact['models']) < len(card['models'])


def test_grouped_context_requires_standard_binding():
    raw=_html([['gpt-5.6-sol',4,.4,5,20]])
    with pytest.raises(ContractError,match='conflict with Standard'):
        rates.parse_openai(raw+_grouped([40,4,50,200,80,8,100,300]))
    # Nonstandard explicit grouping cannot override a Standard table.
    fast=_grouped().replace(b'props="{',b'props="{&quot;tier&quot;:[0,&quot;fast&quot;],',1)
    card=rates.parse_openai(raw+fast)
    assert card['models']['gpt-5.6-sol']['input']==.000004
    assert card['models']['gpt-5.6-sol']['threshold'] is None
    standard=_grouped().replace(b'props="{',b'props="{&quot;tier&quot;:[0,&quot;standard&quot;],',1)
    assert rates.parse_openai(standard)['models']['gpt-5.6-sol']['threshold']==272000
    assert rates.parse_openai(_grouped())['models']['gpt-5.6-sol']['input'] is None


def test_conflicting_context_annotation_is_not_flat_price():
    with pytest.raises(ContractError,match='Conflicting Standard rates'):
        rates.parse_openai(_html([['gpt-test',1,.1,2],['gpt-test (<272K context length)',1,.1,2]]))


def test_missing_provider_selector_never_refreshes(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('Missing provider selector must not start public pricing transport')
    monkeypatch.setattr(httpx,'Client',forbidden)
    context=SimpleNamespace(owner_root=tmp_path)
    assert rates.freeze_pricing(context,{}) is None
    assert rates.cached_rate_card(context,{}) is None
    assert not (tmp_path/'.api-agent').exists()
