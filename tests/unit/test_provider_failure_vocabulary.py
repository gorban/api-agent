"""The provider failure vocabulary is a closed, additive, fail-closed set of fixed tokens."""
from concurrent.futures import ThreadPoolExecutor
import itertools
import threading

import pytest

from api_agent.telemetry import failure_vocabulary as vocabulary
from api_agent.telemetry.failure_vocabulary import (
    DEFAULTS, admits, provider_failure_vocabulary_scope, register_provider_failure_vocabulary)

PUBLIC_DEFAULTS = {
    'type': {'content_policy_violation', 'invalid_request_error', 'rate_limit_error',
             'authentication_error', 'permission_error', 'server_error'},
    'code': {'content_filter', 'context_length_exceeded', 'rate_limit_exceeded',
             'invalid_api_key', 'insufficient_quota'},
    'recommendation': {'block', 'allow', 'review'},
    'categories': {'hate', 'hate/threatening', 'harassment', 'harassment/threatening', 'self-harm',
                   'self-harm/intent', 'self-harm/instructions', 'sexual', 'sexual/minors',
                   'violence', 'violence/graphic', 'illicit', 'illicit/violent'},
    'category_source': {'reported', 'inferred'},
}
FIELD_OF = {'types': 'type', 'codes': 'code', 'categories': 'categories', 'recommendations': 'recommendation'}


@pytest.fixture(autouse=True)
def _restore_vocabulary():
    """Tests that register directly must not leak tokens into other tests."""
    previous = vocabulary._registered
    try:
        yield
    finally:
        vocabulary._publish(previous)


def _snapshot():
    return {field: {token for token in tokens} for field, tokens in vocabulary._admitted.items()}


def test_defaults_are_the_public_provider_independent_names():
    assert {field: set(tokens) for field, tokens in DEFAULTS.items()} == PUBLIC_DEFAULTS


@pytest.mark.parametrize('field,token', [(field, token) for field, tokens in PUBLIC_DEFAULTS.items() for token in sorted(tokens)])
def test_every_default_is_admitted_without_registration(field, token):
    assert admits(field, token) is True


@pytest.mark.parametrize('registration,field', [
    ('types', 'type'), ('codes', 'code'), ('categories', 'categories'), ('recommendations', 'recommendation')])
def test_registered_token_is_admitted_for_its_own_field_only(registration, field):
    assert admits(field, 'example_token') is False

    register_provider_failure_vocabulary(**{registration: ['example_token']})

    assert admits(field, 'example_token') is True
    assert [other for other in PUBLIC_DEFAULTS if other != field and admits(other, 'example_token')] == []


def test_scope_registers_on_entry_and_restores_the_previous_snapshot_on_exit():
    register_provider_failure_vocabulary(categories=['example_outer'])

    with provider_failure_vocabulary_scope(categories=['example_inner']):
        assert admits('categories', 'example_outer') and admits('categories', 'example_inner')

    assert admits('categories', 'example_outer') is True
    assert admits('categories', 'example_inner') is False


def test_scope_exit_keeps_tokens_registered_by_other_threads_while_it_was_open():
    entered, registered, leave = threading.Event(), threading.Event(), threading.Event()

    def scoped():
        with provider_failure_vocabulary_scope(categories=['example_scoped']):
            entered.set()
            assert registered.wait(5)
        leave.set()

    thread = threading.Thread(target=scoped)
    thread.start()
    assert entered.wait(5)
    register_provider_failure_vocabulary(categories=['example_other_thread'], codes=['example_code'])
    registered.set()
    thread.join(5)

    assert leave.is_set()
    assert admits('categories', 'example_scoped') is False
    assert admits('categories', 'example_other_thread') is True
    assert admits('code', 'example_code') is True


def test_scope_exit_keeps_a_token_that_was_registered_before_it_opened():
    register_provider_failure_vocabulary(categories=['example_outer'])

    with provider_failure_vocabulary_scope(categories=['example_outer', 'example_inner']):
        pass

    assert admits('categories', 'example_outer') is True
    assert admits('categories', 'example_inner') is False


def test_scope_restores_even_when_its_body_raises():
    with pytest.raises(RuntimeError):
        with provider_failure_vocabulary_scope(codes=['example_code']):
            raise RuntimeError('body failed')

    assert admits('code', 'example_code') is False


def test_scope_with_invalid_tokens_raises_before_entering_and_changes_nothing():
    before = _snapshot()

    with pytest.raises(ValueError):
        with provider_failure_vocabulary_scope(codes=['Not A Token']):
            pytest.fail('scope must not be entered')  # pragma: no cover

    assert _snapshot() == before


def test_category_source_is_not_registrable():
    with pytest.raises(TypeError):
        register_provider_failure_vocabulary(category_sources=['example_source'])


def test_registering_the_same_token_twice_changes_nothing():
    register_provider_failure_vocabulary(categories=['example_category'])
    once = _snapshot()

    register_provider_failure_vocabulary(categories=['example_category', 'example_category'])

    assert _snapshot() == once


def test_registration_never_removes_or_alters_a_default():
    register_provider_failure_vocabulary(types=['example_type'], codes=['example_code'],
                                         categories=['example_category'], recommendations=['example_review'])

    for field, tokens in PUBLIC_DEFAULTS.items():
        assert tokens <= _snapshot()[field]


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))))
def test_registrations_compose_to_the_same_set_in_any_order(order):
    batches = [dict(categories=['example_a', 'shared_token']), dict(categories=['example_b', 'shared_token']),
               dict(types=['example_type'], codes=['example_code'])]
    with provider_failure_vocabulary_scope():
        for index in order:
            register_provider_failure_vocabulary(**batches[index])
        composed = _snapshot()

    with provider_failure_vocabulary_scope():
        for batch in batches:
            register_provider_failure_vocabulary(**batch)
        assert composed == _snapshot()


@pytest.mark.parametrize('token', ['Free text', 'a' * 65, '', 'UPPER', '1leading_digit', 'has space',
                                   'trailing-', '-leading', 'double//slash', 'new\nline', 'tab\t', 'émoji',
                                   7, None, b'bytes', ('tuple',)])
@pytest.mark.parametrize('registration', ['types', 'codes', 'categories', 'recommendations'])
def test_invalid_token_raises_and_leaves_the_vocabulary_unchanged(registration, token):
    before = _snapshot()

    with pytest.raises(ValueError):
        register_provider_failure_vocabulary(**{registration: ['example_valid', token]})

    assert _snapshot() == before


@pytest.mark.parametrize('container', ['example_token', 7, None, {'example_token': 1}, iter(['example_token'])])
def test_tokens_must_arrive_in_a_list_tuple_or_set(container):
    before = _snapshot()

    with pytest.raises(ValueError):
        register_provider_failure_vocabulary(categories=container)

    assert _snapshot() == before


def test_a_str_subclass_is_not_a_token():
    class Token(str):
        pass
    with pytest.raises(ValueError):
        register_provider_failure_vocabulary(categories=[Token('example_token')])


@pytest.mark.parametrize('token', ['a', 'a' * 64, 'self-harm/intent', 'a_b-c/d_e', 'x1/y2-z3'])
def test_boundary_valid_tokens_register(token):
    register_provider_failure_vocabulary(categories=[token])

    assert admits('categories', token) is True


def test_a_failed_registration_applies_none_of_its_fields():
    before = _snapshot()

    with pytest.raises(ValueError):
        register_provider_failure_vocabulary(types=['example_type'], categories=['Invalid Token'])

    assert _snapshot() == before


def test_at_most_sixty_four_tokens_per_field_may_be_registered():
    register_provider_failure_vocabulary(categories=[f'example_{index}' for index in range(64)])
    before = _snapshot()

    with pytest.raises(ValueError, match='At most 64 categories'):
        register_provider_failure_vocabulary(categories=['example_overflow'])

    assert _snapshot() == before
    register_provider_failure_vocabulary(categories=['example_0'], codes=['example_code'])


def test_the_limit_applies_to_one_oversized_registration_and_never_counts_defaults():
    before = _snapshot()
    with pytest.raises(ValueError, match='At most 64 code tokens'):
        register_provider_failure_vocabulary(codes=[f'example_{index}' for index in range(65)])
    assert _snapshot() == before

    register_provider_failure_vocabulary(categories=sorted(PUBLIC_DEFAULTS['categories']) + [
        f'example_{index}' for index in range(60)])
    assert admits('categories', 'example_59') is True


@pytest.mark.parametrize('field,value', [
    ('categories', 'unregistered'), ('categories', None), ('categories', 7), ('categories', ['violence']),
    ('categories', b'violence'), ('unknown_field', 'violence'), (None, 'violence'), (['categories'], 'violence'),
    ({}, {})])
def test_admits_never_raises_and_rejects_anything_unregistered_or_mistyped(field, value):
    assert admits(field, value) is False


def test_str_subclass_values_are_not_admitted():
    class Token(str):
        pass

    assert admits('categories', Token('violence')) is False


def test_concurrent_registrations_end_at_the_full_union():
    def register(index):
        register_provider_failure_vocabulary(categories=[f'example_{index}'], codes=[f'example_code_{index}'])

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(register, range(48)))

    assert all(admits('categories', f'example_{index}') and admits('code', f'example_code_{index}')
               for index in range(48))
    assert PUBLIC_DEFAULTS['categories'] <= _snapshot()['categories']
