"""Provider receipts expose bounded operational facts without narrative content."""
import pytest

from api_agent.telemetry.failure_vocabulary import provider_failure_vocabulary_scope
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import _structural


FIELDS = {
    'api_agent.provider.failure.class': 'policy_block',
    'api_agent.provider.failure.http_status': 400,
    'api_agent.provider.failure.type': 'content_policy_violation',
    'api_agent.provider.failure.code': 'content_filter',
    'api_agent.provider.failure.recommendation': 'block',
    'api_agent.provider.failure.categories': ['violence'],
    'api_agent.provider.failure.category_source': 'reported',
    'api_agent.provider.retry.attempt': 1,
    'api_agent.provider.retry.total': 2,
    'api_agent.provider.operation': 'example_operation',
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
    ('api_agent.provider.failure.categories', ['violence'] * 17),
    ('api_agent.provider.operation', 'x' * 97),
])
def test_provider_receipt_rejects_out_of_bounds_facts(key, value):
    assert _structural(EmissionBoundary(), {key: value}) == {}


def test_provider_operation_registered_secret_is_redacted():
    boundary = EmissionBoundary()
    boundary.register_secret('registered_operation')
    assert 'registered_operation' not in str(_structural(boundary, {
        'api_agent.provider.operation': 'registered_operation'}))


CODE = 'api_agent.provider.failure.code'
TYPE = 'api_agent.provider.failure.type'
CATEGORIES = 'api_agent.provider.failure.categories'
RECOMMENDATION = 'api_agent.provider.failure.recommendation'
SOURCE = 'api_agent.provider.failure.category_source'


@pytest.mark.parametrize('key,field,token', [
    (TYPE, 'types', 'example_type'),
    (CODE, 'codes', 'example_code'),
    (RECOMMENDATION, 'recommendations', 'example_recommendation'),
])
def test_scalar_token_is_admitted_only_while_registered(key, field, token):
    boundary = EmissionBoundary()
    assert _structural(boundary, {key: token}) == {}

    with provider_failure_vocabulary_scope(**{field: [token]}):
        assert _structural(boundary, {key: token}) == {key: token}

    assert _structural(boundary, {key: token}) == {}


def test_category_is_admitted_only_while_registered_and_not_after_its_scope_exits():
    boundary = EmissionBoundary()
    assert _structural(boundary, {CATEGORIES: ['example_category']}) == {}

    with provider_failure_vocabulary_scope(categories=['example_category']):
        assert _structural(boundary, {CATEGORIES: ['example_category']}) == {CATEGORIES: ['example_category']}
        assert _structural(boundary, {CATEGORIES: ['violence', 'example_category']}) == {
            CATEGORIES: ['violence', 'example_category']}

    assert _structural(boundary, {CATEGORIES: ['example_category']}) == {}


def test_one_unregistered_category_drops_the_whole_list_even_beside_registered_ones():
    with provider_failure_vocabulary_scope(categories=['example_category']):
        assert _structural(EmissionBoundary(), {CATEGORIES: ['example_category', 'unregistered_category']}) == {}


@pytest.mark.parametrize('count', [16, 17])
def test_category_lists_are_bounded_at_sixteen_even_when_every_item_is_registered(count):
    tokens = [f'example_category_{index}' for index in range(count)]
    with provider_failure_vocabulary_scope(categories=tokens):
        actual = _structural(EmissionBoundary(), {CATEGORIES: tokens})

    assert actual == ({CATEGORIES: tokens} if count == 16 else {})


@pytest.mark.parametrize('value', ['violence', ('violence',), [['violence']], [None], [b'violence'], 7])
def test_category_must_be_a_list_of_strings(value):
    assert _structural(EmissionBoundary(), {CATEGORIES: value}) == {}


@pytest.mark.parametrize('value', ['example_source', 'Reported', None, ['reported']])
def test_category_source_admits_only_its_fixed_enumeration(value):
    assert _structural(EmissionBoundary(), {SOURCE: value}) == {}


def test_category_source_cannot_be_widened_by_registration():
    with provider_failure_vocabulary_scope(types=['example_type'], codes=['example_code'],
                                           categories=['example_source'], recommendations=['example_review']):
        assert _structural(EmissionBoundary(), {SOURCE: 'example_source'}) == {}


@pytest.mark.parametrize('content', [False, True])
def test_registered_tokens_survive_either_content_policy(content):
    fields = {CATEGORIES: ['example_category'], CODE: 'example_code'}
    with provider_failure_vocabulary_scope(categories=['example_category'], codes=['example_code']):
        assert _structural(EmissionBoundary(ContentPolicy(content)), fields) == fields


def test_registered_token_equal_to_a_registered_secret_is_still_redacted():
    boundary = EmissionBoundary()
    boundary.register_secret('example_category')
    with provider_failure_vocabulary_scope(categories=['example_category']):
        actual = _structural(boundary, {CATEGORIES: ['example_category'], CODE: 'content_filter'})

    assert 'example_category' not in str(actual)
    assert actual[CODE] == 'content_filter'


def test_registered_scalar_equal_to_a_registered_secret_is_still_redacted():
    boundary = EmissionBoundary()
    boundary.register_secret('example_code')
    with provider_failure_vocabulary_scope(codes=['example_code']):
        actual = _structural(boundary, {CODE: 'example_code'})

    assert 'example_code' not in str(actual)
