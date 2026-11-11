"""`<step>.expired` is an engine-maintained Boolean that only a step with its own Timeouts row may read."""
import pytest

from api_agent.workflows.predicates import PredicateError, edge_expiry, parse_predicate, predicate_fields
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
