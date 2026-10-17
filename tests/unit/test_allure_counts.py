"""Archive fractions describe logical tests and confirmed E2E outcomes."""
import json
from uuid import uuid4

import pytest

from api_agent.testing.allure_counts import result_counts, count_label
from api_agent.testing.allure_source import ViewerError


def rows(statuses, *, family='one', parameters=()):
    return {f'{uuid4()}-result.json': json.dumps({
        'uuid': str(uuid4()), 'historyId': family, 'fullName': family, 'status': status,
        'parameters': [*parameters, {'name': 'attempt', 'value': str(index), 'excluded': True}]
    }).encode() for index, status in enumerate(statuses, 1)}


@pytest.mark.parametrize('mode,statuses,expected,first,final', [
    ('integration', ['failed', 'passed'], None, 0, 1),
    ('integration', ['passed'], None, 1, 1),
    ('e2e', ['passed'], None, 1, 1),
    ('e2e', ['failed'], None, 0, 0),
    ('e2e', ['failed'], 0, 0, 0),
    ('e2e', ['failed', 'passed', 'passed'], 2, 0, 1),
    ('e2e', ['failed', 'passed', 'failed'], 2, 0, 0),
    ('e2e', ['failed', 'passed'], 2, 0, 0),
    ('e2e', ['passed', 'failed'], 2, 1, 0),
    ('e2e', ['failed', 'passed'], None, 0, None),
])
def test_attempt_groups_have_first_and_final_counts(mode, statuses, expected, first, final):
    manifest = {'mode': mode, 'status': 'completed', 'metadata': {'e2e_reruns': expected}}
    assert result_counts(manifest, rows(statuses)) == {'first': first, 'final': final, 'total': 1}


def test_unknown_retry_count_does_not_erase_known_other_tests():
    files = {**rows(['failed', 'passed']), **rows(['passed'], family='two')}
    assert result_counts({'mode': 'e2e', 'status': 'completed'}, files) == {'first': 1, 'final': None, 'total': 2}


def test_semantic_parameters_are_separate_logical_tests():
    files = {**rows(['passed'], parameters=[{'name': 'case', 'value': 'one'}]),
             **rows(['skipped'], parameters=[{'name': 'case', 'value': 'two'}])}
    assert result_counts({'mode': 'integration', 'status': 'completed'}, files) == {'first': 1, 'final': 1, 'total': 2}


def test_ordinary_results_without_attempt_or_history_id_are_counted():
    files = {'one': b'{"uuid":"one","status":"passed"}'}
    assert result_counts({'mode': 'integration', 'status': 'interrupted'}, files) == {'first': 1, 'final': None, 'total': 1}


@pytest.mark.parametrize('body', [
    '{"uuid":"x","uuid":"y","status":"passed"}',
    '{"uuid":"x","status":"passed","parameters":[{"name":"a","value":"x"},{"name":"a","value":"y"}]}',
    '{"uuid":"x","status":"passed","parameters":[{"name":"attempt","value":"0","excluded":true}]}',
    '{"uuid":"x","status":"passed","parameters":[{"name":"attempt","value":1,"excluded":true}]}',
    '{"uuid":"x","status":"passed","parameters":[{"name":"attempt","value":"2","excluded":true}]}',
])
def test_ambiguous_or_incomplete_attempts_are_unavailable(body):
    with pytest.raises(ViewerError):
        result_counts({'mode': 'e2e', 'status': 'completed'}, {'one': body.encode()})


@pytest.mark.parametrize('expected', [-1, True, '2'])
def test_invalid_confirmation_metadata_is_unavailable(expected):
    with pytest.raises(ViewerError, match='confirmation'):
        result_counts({'mode': 'e2e', 'status': 'completed', 'metadata': {'e2e_reruns': expected}}, rows(['passed']))


def test_duplicate_attempts_cannot_be_counted_twice():
    files = {**rows(['passed']), **rows(['failed'])}
    with pytest.raises(ViewerError, match='sequence'):
        result_counts({'mode': 'integration', 'status': 'completed'}, files)


@pytest.mark.parametrize('mode,counts,label', [
    ('integration', {'first': 1, 'final': 1, 'total': 3}, '1 / 3 passed'),
    ('integration', {'first': 0, 'final': None, 'total': 3}, 'pass counts unavailable'),
    ('e2e', {'first': 1, 'final': 2, 'total': 3}, 'first 1 / 3 (33.3%) — final 2 / 3 (66.7%)'),
    ('e2e', {'first': 0, 'final': None, 'total': 0}, 'first 0 / 0 (0.0%) — final unavailable'),
])
def test_archive_count_labels(mode, counts, label):
    assert count_label(mode, counts) == label


def test_other_excluded_parameters_do_not_change_logical_identity():
    files = rows(['passed'], parameters=[{'name': 'request_id', 'value': 'one', 'excluded': True}])
    assert result_counts({'mode': 'integration', 'status': 'completed'}, files) == {'first': 1, 'final': 1, 'total': 1}


def test_attempts_beyond_the_recorded_confirmation_policy_are_unavailable():
    with pytest.raises(ViewerError, match='exceed'):
        result_counts({'mode': 'e2e', 'status': 'completed', 'metadata': {'e2e_reruns': 0}}, rows(['failed', 'passed']))


def test_display_flags_do_not_split_a_logical_test_between_attempts():
    files = rows(['failed', 'passed'], parameters=[{'name': 'case', 'value': 'one'}])
    name = next(reversed(files))
    second = json.loads(files[name])
    second['parameters'][0].update(excluded=False, mode='masked')
    files[name] = json.dumps(second).encode()
    assert result_counts({'mode': 'integration', 'status': 'completed'}, files) == {'first': 0, 'final': 1, 'total': 1}
