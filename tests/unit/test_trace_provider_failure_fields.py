"""Provider receipts expose bounded operational facts without narrative content."""
import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import _structural


FIELDS = {
    'api_agent.provider.failure.class': 'policy_block',
    'api_agent.provider.failure.http_status': 400,
    'api_agent.provider.failure.type': 'content_policy_violation',
    'api_agent.provider.failure.code': 'content_blocked',
    'api_agent.provider.failure.recommendation': 'block',
    'api_agent.provider.failure.categories': ['jailbreak_attempt'],
    'api_agent.provider.retry.attempt': 1,
    'api_agent.provider.retry.total': 2,
    'api_agent.provider.operation': 'introspect_and_shape_queries_tool_context',
}


@pytest.mark.parametrize('content', [False, True])
def test_safe_provider_receipt_survives_content_policy(content):
    boundary = EmissionBoundary(ContentPolicy(content))
    assert _structural(boundary, FIELDS) == FIELDS


@pytest.mark.parametrize('key', list(FIELDS))
@pytest.mark.parametrize('value', [None, True, {}, 'sensitive provider narrative'])
def test_provider_receipt_rejects_unadmitted_values(key, value):
    assert _structural(EmissionBoundary(), {key: value}) == {}


@pytest.mark.parametrize('key,value', [
    ('api_agent.provider.failure.http_status', 99),
    ('api_agent.provider.failure.http_status', 600),
    ('api_agent.provider.retry.attempt', 0),
    ('api_agent.provider.retry.total', 101),
    ('api_agent.provider.failure.categories', ['unknown-category']),
    ('api_agent.provider.failure.categories', ['jailbreak_attempt'] * 17),
    ('api_agent.provider.operation', 'x' * 97),
])
def test_provider_receipt_rejects_out_of_bounds_facts(key, value):
    assert _structural(EmissionBoundary(), {key: value}) == {}


def test_provider_operation_registered_secret_is_redacted():
    boundary = EmissionBoundary()
    boundary.register_secret('registered_operation')
    assert 'registered_operation' not in str(_structural(boundary, {
        'api_agent.provider.operation': 'registered_operation'}))
