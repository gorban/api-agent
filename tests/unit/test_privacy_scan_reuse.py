"""Reused scan results for unchanged content must export exactly like a fresh scan."""
import json
import math

import pytest

from api_agent.telemetry import privacy
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

SECRET = 'registered-secret-value-0123456789'
OTHER = 'second-registered-secret-abcdef'
FILLER = 'Plain prose with {braces}, "quotes", [brackets] and Bearer looking words. ' * 60


def history(extra=''):
    tool = json.dumps({'data': {'items': [{'name': f'item{index}', 'note': FILLER[:200]} for index in range(30)]},
                       'nested': json.dumps({'token': SECRET, 'text': 'inner ' + OTHER})})
    return {'llm_request': {
        'messages': [
            {'role': 'system', 'content': [{'text': FILLER}]},
            {'role': 'user', 'content': [{'text': 'Find {"api_key": "' + SECRET + '"} and Bearer abc.def.ghi ' + FILLER}]},
            {'role': 'assistant', 'calls': [{'name': 'lookup', 'arguments': json.dumps({'password': 'x', 'q': FILLER})}]},
            {'role': 'tool', 'results': [{'content': [{'text': tool}]}]},
            {'role': 'user', 'content': [{'text': 'More ' + extra + FILLER}]},
        ],
        'authorization': 'Bearer ' + SECRET,
        'parts': ['ab', SECRET[:10], SECRET[10:], FILLER],
        'model': 'selected',
    }}


@pytest.fixture
def fresh():
    """Exports with reuse disabled, for comparison."""
    def export(boundary, payload, method='content_export'):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(privacy, '_SCAN_REUSE_MIN_CHARS', 10 ** 12)
            return getattr(boundary, method)(payload)
    privacy.clear_scan_reuse()
    yield export
    privacy.clear_scan_reuse()


def boundary(*secrets, policy=True, preserve=False):
    return EmissionBoundary(ContentPolicy(policy), secrets=secrets, preserve_json_text=preserve)


@pytest.mark.parametrize('method', ['export', 'content_export', 'redacted_content'])
@pytest.mark.parametrize('policy', [True, False])
@pytest.mark.parametrize('preserve', [True, False])
def test_reused_results_equal_a_fresh_scan_across_modes(fresh, method, policy, preserve):
    owner = boundary(SECRET, OTHER, policy=policy, preserve=preserve)
    expected = fresh(owner, history(), method)
    first = getattr(owner, method)(history())
    assert bool(privacy._SCAN_REUSE) == (policy or method != 'export')
    second = getattr(owner, method)(history())
    grown = getattr(owner, method)(history('grown '))
    assert first == expected and second == expected
    assert grown == fresh(owner, history('grown '), method)


def test_a_secret_added_or_removed_between_exports_is_honored(fresh):
    one = boundary(SECRET)
    assert one.content_export(history()) == fresh(one, history())
    both = boundary(SECRET, OTHER)
    assert both.content_export(history()) == fresh(both, history())
    assert OTHER not in json.dumps(both.content_export(history()))
    owner = boundary(SECRET)
    scoped = owner.credential_owner()
    scoped.register(OTHER)
    assert owner.content_export(history()) == fresh(owner, history())
    scoped.retire()
    assert owner.content_export(history()) == fresh(owner, history())


def test_mutating_an_export_never_changes_a_later_one(fresh):
    owner = boundary(SECRET)
    first = owner.content_export(history())
    first['llm_request']['messages'][0]['content'][0]['text'] = 'changed'
    first['llm_request']['messages'].append('extra')
    assert owner.content_export(history()) == fresh(owner, history())


def test_values_that_json_would_rewrite_are_never_reused(fresh):
    class Count(int):
        pass

    deep = [FILLER]
    for _ in range(60):
        deep = [deep]
    loop = [FILLER]
    loop.append(loop)
    owner = boundary(SECRET)
    payload = {'llm_request': {'loop': loop, 'tuple': ('a', FILLER), 'numbers': {1: FILLER, '1': 'text ' + FILLER},
                               'nan': [math.nan, FILLER], 'list': [FILLER, ('x',)],
                               'opaque': [object(), FILLER], 'subclass': [Count(7), FILLER], 'deep': deep}}
    expected = fresh(owner, payload)
    assert owner.content_export(payload) == expected
    assert owner.content_export(payload) == expected
    assert {key[0] for key in privacy._SCAN_REUSE} == {'text'}


def test_reuse_stays_within_its_entry_and_character_bounds(fresh, monkeypatch):
    monkeypatch.setattr(privacy, '_SCAN_REUSE_MAX_ENTRIES', 3)
    monkeypatch.setattr(privacy, '_SCAN_REUSE_MAX_CHARS', 30000)
    owner = boundary(SECRET)
    for index in range(10):
        payload = {'llm_request': {'text': f'{index} ' + FILLER, 'more': [f'{index}', FILLER]}}
        assert owner.content_export(payload) == fresh(owner, payload)
        assert len(privacy._SCAN_REUSE) <= 3
        assert privacy._SCAN_REUSE_SIZE <= 30000
    oversized = {'llm_request': {'text': 'x' * 40000}}
    assert owner.content_export(oversized) == fresh(owner, oversized)
    assert privacy._SCAN_REUSE_SIZE <= 30000
