"""Exact ranges coalesce dense matches and retain overlapping suffixes."""
import pytest

from api_agent.telemetry.redaction import overlapping_ranges


@pytest.mark.parametrize('text, secret, expected', [
    ('', 'x', []),
    ('short', 'longer-value', []),
    ('abc abc', 'abc', [(0, 3), (4, 7)]),
    ('abcabc', 'abc', [(0, 6)]),
    ('ababa', 'aba', [(0, 5)]),
    ('ababababaca', 'ababaca', [(4, 11)]),
    ('aaaaaaaaab', 'aaaaab', [(4, 10)]),
    ('abx ab', 'abc', []),
    ('秘密秘密密', '秘密密', [(2, 5)]),
    ('ééé', 'éé', [(0, 3)]),
])
def test_exact_range_union_preserves_overlap_and_disjoint_matches(text, secret, expected):
    assert list(overlapping_ranges(text, secret)) == expected


def test_empty_matching_value_is_rejected():
    with pytest.raises(ValueError, match='must be nonempty'):
        list(overlapping_ranges('ordinary text', ''))


def test_dense_overlapping_matches_emit_only_their_union():
    assert list(overlapping_ranges('A' * 100000, 'A' * 50000)) == [(0, 100000)]
