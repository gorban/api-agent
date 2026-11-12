"""`<step>.expired` is an engine-maintained Boolean that only a step with its own Timeouts row may read."""
import pytest

from api_agent.workflows.predicates import (PredicateError, backward_bound, edge_effected, edge_expiry,
                                            parse_predicate, predicate_fields)
from expiry_support import expiring
from loop_support import load


@pytest.mark.parametrize('when,expected', [
    ('work.expired', 'requires'),
    ('(work.expired)', 'requires'),
    ('work["expired"]', 'requires'),
    ('work.expired and work.attempt < NODE_TIMEOUT_PASSES', 'requires'),
    ('work.attempt < NODE_TIMEOUT_PASSES and work.expired', 'requires'),
    ('not work.expired', 'reads'),
    ('work.expired or work.attempt > 3', 'reads'),
    ('work.attempt > 3', None),
    ('other.expired', None),
    ('work.n == 1', None),
    ('always', None),
    ('done_check', None),
    ('work.expired and', None),
    ('"unterminated', None),
])
def test_edge_expiry_classifies_how_a_condition_uses_the_flag(when, expected):
    assert edge_expiry(when, 'work') == expected


def fields(tmp_path, owners=(('work', 'NODE_TIMEOUT'),)):
    return predicate_fields(load(expiring(tmp_path, ('work', 'finish', 'always'), owners=owners)).layout, 'work')


def test_the_flag_reads_false_until_the_step_expires(tmp_path):
    predicate = parse_predicate('work.expired', fields(tmp_path))

    assert predicate.evaluate({}) is False
    assert predicate.evaluate({}, expired={'finish': True}) is False
    assert predicate.evaluate({}, expired={'work': False}) is False
    assert predicate.evaluate({}, expired={'work': True}) is True
    assert parse_predicate('not work.expired', fields(tmp_path)).evaluate({}, expired={'work': True}) is False


def test_the_flag_combines_with_start_counters(tmp_path):
    predicate = parse_predicate('work.expired and work.attempt < NODE_TIMEOUT_PASSES', fields(tmp_path))

    assert predicate.evaluate({}, expired={'work': True}, visits={'work': 1}, constants={'NODE_TIMEOUT_PASSES': 2})
    assert not predicate.evaluate({}, expired={'work': True}, visits={'work': 2}, constants={'NODE_TIMEOUT_PASSES': 2})


@pytest.mark.parametrize('owners', [(), (('finish', 'NODE_TIMEOUT'),), (('GRAPH', 'GRAPH_TIMEOUT'),)])
def test_only_a_step_with_its_own_timeouts_row_has_the_flag(tmp_path, owners):
    with pytest.raises(PredicateError, match='work.expired requires work to declare its own Timeouts row'):
        parse_predicate('work.expired', fields(tmp_path, owners))


def test_the_internal_expiry_name_is_reserved():
    with pytest.raises(PredicateError, match='reserved condition name'):
        parse_predicate('__route_expiring__', None)


def test_the_expiry_count_reads_zero_until_the_step_expires(tmp_path):
    predicate = parse_predicate('work.expirations < 2', fields(tmp_path))

    assert predicate.evaluate({}) is True
    assert predicate.evaluate({}, expirations={'work': 1}) is True
    assert predicate.evaluate({}, expirations={'work': 2}) is False
    assert predicate.evaluate({}, expirations={'finish': 5}) is True


def test_the_expiry_count_is_independent_of_the_start_count(tmp_path):
    predicate = parse_predicate('work.attempt >= 3 and work.expirations < 1', fields(tmp_path))

    assert predicate.evaluate({}, visits={'work': 3}, expirations={'work': 0}) is True
    assert predicate.evaluate({}, visits={'work': 3}, expirations={'work': 1}) is False


@pytest.mark.parametrize('owners', [(), (('finish', 'NODE_TIMEOUT'),), (('GRAPH', 'GRAPH_TIMEOUT'),)])
def test_only_a_step_with_its_own_timeouts_row_has_the_count(tmp_path, owners):
    with pytest.raises(PredicateError, match='work.expirations requires work to declare its own Timeouts row'):
        parse_predicate('work.expirations < 2', fields(tmp_path, owners))


@pytest.mark.parametrize('when,source,target,bound', [
    ('work.attempt < LIMIT', 'review', 'work', ('LIMIT', False, 'attempt')),
    ('work.attempt <= LIMIT', 'work', 'work', ('LIMIT', True, 'attempt')),
    ('work.expired and work.expirations < LIMIT', 'work', 'work', ('LIMIT', False, 'expirations')),
    ('work.expirations <= LIMIT and work.expired', 'work', 'work', ('LIMIT', True, 'expirations')),
    ('work.expirations < LIMIT', 'work', 'work', None),
    ('not work.expired and work.expirations < LIMIT', 'work', 'work', None),
    ('work.expired or work.expirations < LIMIT', 'work', 'work', None),
    ('work.expired and work.expirations < LIMIT', 'review', 'work', None),
    ('work.expired and work.expirations < OTHER', 'work', 'work', None),
    ('work.expired and other.expirations < LIMIT', 'work', 'work', None),
])
def test_an_expiry_count_bounds_only_the_replay_of_the_step_that_expired(when, source, target, bound):
    predicate = parse_predicate(when, None)

    assert backward_bound(predicate, source, target, {'LIMIT'}) == bound


@pytest.mark.parametrize('when,expired,effected', [
    ('work.effected', 'requires', True),
    ('work.expired and work.effected', 'requires', True),
    ('work.expired and not work.effected', 'requires', False),
    ('not work.effected', 'reads', False),
    ('work.effected or work.attempt > 3', 'reads', False),
    ('other.effected', None, False),
    ('work.attempt > 3', None, False),
    ('work.effected and', None, False),
])
def test_an_effected_condition_counts_as_reading_the_expiry(when, expired, effected):
    assert edge_expiry(when, 'work') == expired
    assert edge_effected(when, 'work') is effected


def test_the_effected_flag_reads_false_unless_the_expiry_followed_an_effect(tmp_path):
    predicate = parse_predicate('work.effected', fields(tmp_path))

    assert predicate.evaluate({}) is False
    assert predicate.evaluate({}, expired={'work': True}) is False
    assert predicate.evaluate({}, effected={'finish': True}) is False
    assert predicate.evaluate({}, effected={'work': True}) is True


@pytest.mark.parametrize('owners', [(), (('finish', 'NODE_TIMEOUT'),), (('GRAPH', 'GRAPH_TIMEOUT'),)])
def test_only_a_step_with_its_own_timeouts_row_has_the_effected_flag(tmp_path, owners):
    with pytest.raises(PredicateError, match='work.effected requires work to declare its own Timeouts row'):
        parse_predicate('work.effected', fields(tmp_path, owners))
