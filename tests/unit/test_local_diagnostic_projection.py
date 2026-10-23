"""Local presentation never recovers source-withheld or unsupported values."""
import json
from types import MappingProxyType
import pytest
from api_agent.runtime.local_diagnostics import LocalDiagnosticProjection


def render(row, full=False):
    return json.loads(LocalDiagnosticProjection(full_content=full).format(MappingProxyType(row)))


def test_structural_identity_is_preserved_and_controls_are_literal():
    row = dict(event='scope_start', event_id='event-1', invocation_id='invocation-1', owner='node\x1b[31m', unexpected='not admitted')
    text = LocalDiagnosticProjection().format(MappingProxyType(row))
    assert '\x1b' not in text
    assert json.loads(text) == {key: row[key] for key in ('event', 'owner', 'event_id', 'invocation_id')}


@pytest.mark.parametrize('full', [False, True])
def test_content_withheld_remains_withheld(full):
    assert render(dict(event='capture', reason='withheld'), full)['content'] == 'withheld'


@pytest.mark.parametrize('full,length', [(False, 512), (True, 700)])
def test_preview_and_full_preserve_source_truncation(full, length):
    result = render(dict(event='capture', reason='truncated', values=MappingProxyType({'text': 'x' * 700}), truncated_fields=('text',)), full)
    assert len(result['values']['text']) == length
    assert result['truncated_fields'] == ['text']
    assert result['source_reason'] == 'truncated'


def test_plain_scalars_and_unknown_nested_payload_are_distinct():
    result = render(dict(event='capture', values=MappingProxyType({'ok': True, 'n': 1, 'none': None, 'nested': ('hidden',)})))
    assert result['values'] == {'ok': True, 'n': 1, 'none': None}
    assert result['omitted_fields'] == ['nested']
    assert result['content'] == 'unsupported_schema'


def test_unknown_capture_schema_is_omitted():
    assert render(dict(event='capture', values=('opaque',)))['content'] == 'unsupported_schema'


def test_plain_metadata_thaws_without_object_introspection():
    assert render(dict(event='call', elapsed=MappingProxyType({'samples': (1, 2)})))['elapsed'] == {'samples': [1, 2]}
    with pytest.raises(ValueError, match='Unsupported'):
        render(dict(event='call', elapsed=object()))
    with pytest.raises(ValueError, match='mapping'):
        LocalDiagnosticProjection().format(object())


def test_admitted_attempt_route_budget_and_supplied_accounting_remain_visible():
    row = MappingProxyType(dict(event='call', attempt=2, max_passes=3,
        declared_timeout=20, effective_timeout=7, timeout_key='NODE_TIMEOUT',
        source='first', target='second', condition='always', implicit=True,
        selection='sample-model', accounting='central_call_id',
        usage=MappingProxyType(dict(input_tokens=11, output_tokens=7,
            estimated_cost_usd=.01, basis='provider_estimate')),
        started_ms=1000, stopped_ms=2000, accepted=True, cause_id='cause-1'))
    result = json.loads(LocalDiagnosticProjection().format(row))
    assert result == dict(row, usage=dict(row['usage']))
    assert 'values' not in result
    assert 'charged_cost_usd' not in result['usage']


def test_structural_event_does_not_invent_usage_or_retain_unadmitted_payload():
    result = render(dict(event='scope_start', attempt=1, max_passes=2,
        effective_timeout=3, inputs='withheld input', outputs='withheld output'))
    assert result == dict(event='scope_start', attempt=1, max_passes=2, effective_timeout=3)
    assert 'usage' not in result
