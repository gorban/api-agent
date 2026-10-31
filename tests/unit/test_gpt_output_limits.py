"""GPT validation envelopes establish output ceilings without inference replay."""
import httpx
import pytest

from api_agent.llm_providers.gpt.output_limits import output_rejection
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.output_limits import OutputLimitRejected
from test_gpt_inference import request, run


def envelope(field='max_output_tokens', maximum=8192):
    return {'error': {'type': 'invalid_request_error', 'code': 'integer_above_max_value',
        'param': field, 'message': f"Invalid '{field}': integer above maximum value. Expected a value <= {maximum}, but got 200000 instead."}}


@pytest.mark.parametrize('mode,field', [('responses', 'max_output_tokens'), ('completions', 'max_completion_tokens')])
@pytest.mark.parametrize('status', [400, 422])
def test_http_parameter_maximum_is_a_safe_preinference_fact(mode, field, status, monkeypatch):
    from api_agent.llm_providers.gpt import inference
    async def forbid_capture(*args, **kwargs):
        pytest.fail('original rejection must not be captured')
    monkeypatch.setattr(inference, '_capture_safe_response', forbid_capture)
    with pytest.raises(OutputLimitRejected) as caught:
        run(httpx.Response(status, json=envelope(field)), api_mode=mode, req=request(200000))
    assert caught.value.maximum_tokens == 8192
    assert caught.value.execution == 'not_started'
    assert caught.value.usage is None
    assert caught.value.http_status == status
    assert '200000' not in str(vars(caught.value))


@pytest.mark.parametrize('status', [None, True, '400', 200, 401, 429, 500, 503])
def test_status_is_required_validation_evidence(status):
    assert output_rejection(status, envelope(), subscription=False, api_mode='responses') is None


@pytest.mark.parametrize('change', [
    {'param': 'max_completion_tokens'}, {'param': None}, {'type': 'server_error'},
    {'code': 'context_length_exceeded'}, {'usage': {}}, {'output': []},
    {'message': 'maximum context length is 8192 tokens'},
    {'message': 'max_output_tokens: 200000 is greater than the maximum of 8192; context overflow'},
    {'message': None}, {'message': 8192}, {'message': 'x' * 513},
])
def test_ambiguous_or_inference_error_stays_untyped(change):
    data = envelope()
    data['error'].update(change)
    assert output_rejection(400, data, subscription=False, api_mode='responses') is None


@pytest.mark.parametrize('data', [None, [], {}, {'error': None}, {'error': []},
    dict(envelope(), usage={}), dict(envelope(), output=[]), dict(envelope(), choices=[])])
def test_only_validation_envelope_without_inference_evidence_is_admitted(data):
    assert output_rejection(400, data, subscription=False, api_mode='responses') is None


@pytest.mark.parametrize('maximum', [0, -1, 1048577])
def test_invalid_maximum_stays_untyped(maximum):
    assert output_rejection(400, envelope(maximum=maximum), subscription=False, api_mode='responses') is None


def test_subscription_never_normalizes_a_limit():
    assert output_rejection(400, envelope(), subscription=True, api_mode='responses') is None


@pytest.mark.parametrize('message', ['max_output_tokens: 200000 is greater than the maximum of 8192',
    'max_output_tokens must be between 1 and 8192.'])
def test_strict_alternative_maximum_messages(message):
    data = envelope()
    data['error']['message'] = message
    assert output_rejection(400, data, subscription=False, api_mode='responses').maximum_tokens == 8192


@pytest.mark.parametrize('status,change', [(500, {}), (400, {'code': 'context_length_exceeded'}),
    (400, {'message': 'maximum context length is 8192 tokens'})])
def test_http_ambiguous_errors_do_not_become_corrections(status, change):
    data = envelope()
    data['error'].update(change)
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(status, json=data), req=request(200000))
    assert not isinstance(caught.value, OutputLimitRejected)
    assert caught.value.execution == 'possibly_executed'
