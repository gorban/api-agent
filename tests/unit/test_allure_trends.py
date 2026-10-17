"""Native future Trend data never overwrites selected results or History."""
import json
from pathlib import Path

import pytest

from api_agent.testing import allure_trends as trends
from api_agent.testing.allure_source import ViewerError


def entry(number):
    return {'buildOrder': number, 'reportUrl': f'http://127.0.0.1/run/{number}',
            'reportName': str(number), 'data': {'passed': 1}}


@pytest.fixture
def widgets(tmp_path):
    before, after, target = (tmp_path / name for name in ('before', 'after', 'target'))
    for root in (before, after, target):
        (root / 'widgets').mkdir(parents=True)
        (root / 'history').mkdir()
        (root / 'history/history.json').write_bytes(b'unchanged test history')
        (root / 'test-result.json').write_bytes(b'unchanged selected result')
    for name in trends.TREND_FILES:
        (before / 'widgets' / name).write_text(json.dumps([entry(n) for n in range(19, -1, -1)]))
        (after / 'widgets' / name).write_text(json.dumps([entry(n) for n in range(38, 18, -1)]))
    return before, after, target


def test_surrounding_widgets_have_19_earlier_and_later_launches(widgets):
    before, after, target = widgets
    trends.surrounding_widgets(before, after, target)
    for name in trends.TREND_FILES:
        rows = json.loads((target / 'widgets' / name).read_bytes())
        assert [row['buildOrder'] for row in rows] == list(range(38, -1, -1))
    assert (target / 'history/history.json').read_bytes() == b'unchanged test history'
    assert (target / 'test-result.json').read_bytes() == b'unchanged selected result'
    assert json.loads((before / 'widgets/history-trend.json').read_bytes())[0]['buildOrder'] == 19


def test_reports_with_no_trend_widgets_keep_original_contents(widgets):
    before, after, target = widgets
    for root in (before, after):
        for path in (root / 'widgets').iterdir():
            path.unlink()
    trends.surrounding_widgets(before, after, target)
    assert list((target / 'widgets').iterdir()) == []


@pytest.mark.parametrize('body', ['{}', '[null]', '[{"buildOrder":true,"data":{}}]',
    '[{"buildOrder":1,"reportUrl":1,"data":{}}]',
    '[{"buildOrder":1,"reportUrl":"x","data":[]}]'])
def test_invalid_generated_trends_fail_before_publication(widgets, body):
    before, after, target = widgets
    (before / 'widgets/history-trend.json').write_text(body)
    with pytest.raises(ViewerError):
        trends.surrounding_widgets(before, after, target)


def test_conflicting_native_entries_are_rejected(widgets):
    before, after, target = widgets
    (after / 'widgets/history-trend.json').write_text(json.dumps([dict(entry(1), data={'failed': 1})]))
    with pytest.raises(ViewerError, match='Conflicting'):
        trends.surrounding_widgets(before, after, target)


def test_surrounding_count_is_bounded(widgets):
    before, after, target = widgets
    (after / 'widgets/history-trend.json').write_text(json.dumps([entry(n) for n in range(19, 41)]))
    with pytest.raises(ViewerError, match='limit'):
        trends.surrounding_widgets(before, after, target)


def test_generated_input_size_is_bounded_before_json_decode(widgets, monkeypatch):
    monkeypatch.setattr(trends, 'MAX_ARTIFACT', 3)
    with pytest.raises(ViewerError, match='size'):
        trends.surrounding_widgets(*widgets)


def test_merged_output_size_is_bounded(widgets, monkeypatch):
    before, after, target = widgets
    size = max((before / 'widgets/history-trend.json').stat().st_size,
               (after / 'widgets/history-trend.json').stat().st_size)
    monkeypatch.setattr(trends, 'MAX_ARTIFACT', size)
    with pytest.raises(ViewerError, match='size'):
        trends.surrounding_widgets(before, after, target)


def test_one_sided_missing_widget_is_not_treated_as_empty_history(widgets):
    before, after, target = widgets
    (before / 'widgets/history-trend.json').unlink()
    with pytest.raises(FileNotFoundError):
        trends.surrounding_widgets(before, after, target)


def test_symlinked_widget_directory_is_rejected(widgets, tmp_path):
    before, after, target = widgets
    (before / 'widgets').rename(tmp_path / 'external')
    (before / 'widgets').symlink_to(tmp_path / 'external', target_is_directory=True)
    with pytest.raises(ViewerError, match='Symlinked'):
        trends.surrounding_widgets(before, after, target)


def test_broken_widget_symlink_is_not_skipped(widgets):
    before, after, target = widgets
    path = after / 'widgets/history-trend.json'
    path.unlink()
    path.symlink_to(after / 'missing')
    with pytest.raises(OSError):
        trends.surrounding_widgets(before, after, target)


def test_optional_linkage_metadata_and_identical_rows_keep_launch_multiplicity(widgets):
    before, after, target = widgets
    for root in (before, after):
        for name in trends.TREND_FILES:
            (root / 'widgets' / name).write_text(json.dumps([{'data': {'passed': 1}}] * 20))
    trends.surrounding_widgets(before, after, target)
    assert len(json.loads((target / 'widgets/history-trend.json').read_bytes())) == 39


def test_nonchronological_build_numbers_do_not_reorder_the_native_chain(widgets):
    before, after, target = widgets
    for name in trends.TREND_FILES:
        (before / 'widgets' / name).write_text(json.dumps([entry(99), entry(5)]))
        (after / 'widgets' / name).write_text(json.dumps([entry(1), entry(99), entry(5)]))
    trends.surrounding_widgets(before, after, target, 1)
    assert [row['buildOrder'] for row in json.loads((target / 'widgets/history-trend.json').read_bytes())] == [1, 99, 5]


def test_mismatched_native_overlap_is_rejected(widgets):
    before, after, target = widgets
    changed = [entry(n) for n in range(38, 18, -1)]
    changed[-1]['data'] = {'failed': 1}
    (after / 'widgets/history-trend.json').write_text(json.dumps(changed))
    with pytest.raises(ViewerError, match='Conflicting'):
        trends.surrounding_widgets(before, after, target)


@pytest.mark.parametrize('later', [0, 20, True])
def test_surrounding_window_is_bounded(widgets, later):
    with pytest.raises(ViewerError, match='window'):
        trends.surrounding_widgets(*widgets, later)


@pytest.mark.parametrize('count', [0, True, 2.5, None])
def test_invalid_configured_trend_window_is_rejected(widgets, count):
    with pytest.raises(ViewerError, match='window'):
        trends.surrounding_widgets(*widgets, 1, count)
