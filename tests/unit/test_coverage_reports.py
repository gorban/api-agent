"""Coverage preview controls and retained evidence, without paid/external runs."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.coverage_reports import coverage_table, retain_report
from api_agent.testing.runner import CoverageArea, format_unit_summary, run_unit_tests
from api_agent.config import load_shared_schema
from api_agent.config.declarations import ConfigSchema
from api_agent.config.resolve import resolve_config


def area(index=0, *, complete=False, missing_lines=(2,), missing_branches=((3, 5),)):
    return CoverageArea('base', f'src/file_{index:02}.py', 2 if complete else 1, 2,
                        2 if complete else 1, 2, missing_lines, missing_branches)


def test_preview_lists_only_twenty_gaps_and_counts_remaining_files():
    areas = tuple(area(index) for index in range(25)) + (area(99, complete=True),)

    output = '\n'.join(coverage_table(areas))

    assert output.count('base::') == 20
    assert 'file_19.py' in output and 'file_20.py' not in output
    assert 'file_99.py' not in output
    assert '… 5 more files below 100%; 1 files fully covered.' in output
    assert '2; branches 3→5' in output


def test_full_table_includes_complete_files_and_unabridged_missing_details():
    gaps = tuple(range(100))
    areas = tuple(area(index, missing_lines=gaps) for index in range(21)) + (area(99, complete=True),)

    output = '\n'.join(coverage_table(areas, full=True))

    assert output.count('base::') == 22
    assert 'file_99.py' in output and '100.00%' in output
    assert ', '.join(map(str, gaps)) in output
    assert '…' not in output
    assert 'Coverage: 21 files below 100%; 1 files fully covered.' in output


@pytest.mark.parametrize('full, expected', [(False, []), (True, [])])
def test_empty_scope_has_no_invented_percentage(full, expected):
    assert coverage_table((), full=full) == expected


def test_preview_abbreviates_long_paths_and_missing_details():
    long = CoverageArea('base', 'src/' + 'a' * 100 + '.py', 1, 2, 0, 0,
                        tuple(range(100)))

    output = '\n'.join(coverage_table((long,)))

    assert output.count('…') == 2
    assert 'branches ' not in output.splitlines()[1]  # no missing branch arcs
    assert '100.00%' in output  # no branch opportunities


def test_full_output_preserves_long_file_names():
    long = CoverageArea('base', 'src/' + 'a' * 100 + '.py', 1, 2, 0, 0)

    output = '\n'.join(coverage_table((long,), full=True))

    assert long.file in output
    assert '…' not in output


def test_preview_does_not_treat_rounded_percentages_as_complete():
    almost = CoverageArea('base', 'src/almost.py', 99999, 100000, 0, 0)

    output = '\n'.join(coverage_table((almost,)))

    assert 'src/almost.py' in output
    assert '<100%' in output
    assert 'Coverage: 1 files below 100%; 0 files fully covered.' in output
    assert '100% · branches 100%' not in output


def test_retain_report_copies_raw_data_json_and_html_without_overwriting(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    (scratch / '.coverage').write_bytes(b'coverage database')
    (scratch / 'coverage.json').write_text('{"files":{}}')
    (scratch / 'html').mkdir()
    (scratch / 'html/index.html').write_text('<html>coverage</html>')
    destination = tmp_path / 'retained'

    report = retain_report('base', scratch, destination)

    assert report.repo == 'base'
    assert report.json_path.read_text() == '{"files":{}}'
    assert report.html_path.read_text() == '<html>coverage</html>'
    assert (destination / '.coverage').read_bytes() == b'coverage database'
    with pytest.raises(FileExistsError):
        retain_report('base', scratch, destination)


def test_real_partial_run_keeps_missing_lines_branches_and_one_table(tmp_path):
    base = tmp_path / 'base'
    (base / 'src').mkdir(parents=True)
    (base / 'src/example.py').write_text('def choose(value):\n    if value:\n        return 1\n    return 2\n')
    (base / 'tests/unit').mkdir(parents=True)
    (base / 'tests/unit/test_example.py').write_text('from example import choose\ndef test_ok():\n    assert choose(True) == 1\n')
    context = SimpleNamespace(base_root=base, owner_root=base,
                              suite_paths=lambda mode: [base / 'tests/unit'])

    result = run_unit_tests(context, collect_tests(context, 'unit'), require_full_coverage=False)

    assert result.exit_code == 0
    assert 'base::src/example.py' not in result.output
    assert 'Coverage JSON written' not in result.output
    assert 'TOTAL' not in result.output
    assert '1 passed' in result.output
    assert len(result.coverage_reports) == 1
    report = result.coverage_reports[0]
    data = json.loads(report.json_path.read_text())
    row = next(iter(data['files'].values()))
    assert row['missing_lines'] == [4]
    assert row['missing_branches'] == [[2, 4]]
    assert report.html_path.is_file()
    assert (report.json_path.parent / '.coverage').is_file()
    assert report.json_path.is_relative_to(base / 'artifacts/unit')
    assert not list(report.json_path.parent.glob('events.jsonl'))
    summary = format_unit_summary(result)
    assert summary.count('base::src/example.py') == 1
    assert '4; branches 2→4' in summary
    assert str(report.json_path) in summary and str(report.html_path) in summary
    assert 'Partial selection: coverage is informational.' in summary


def test_real_full_option_survives_result_and_uses_same_summary(tmp_path):
    base = tmp_path / 'base'
    (base / 'src').mkdir(parents=True)
    (base / 'src/example.py').write_text('VALUE = 1\n')
    (base / 'tests/unit').mkdir(parents=True)
    (base / 'tests/unit/test_example.py').write_text('from example import VALUE\ndef test_ok():\n    assert VALUE == 1\n')
    context = SimpleNamespace(base_root=base, owner_root=base,
                              suite_paths=lambda mode: [base / 'tests/unit'])

    result = run_unit_tests(context, collect_tests(context, 'unit'), coverage_full=True)

    assert result.exit_code == 0 and result.coverage_full
    assert format_unit_summary(result).count('base::src/example.py') == 1
    assert 'Code coverage: lines 100% · branches 100%.' not in format_unit_summary(result)


@pytest.mark.parametrize('dotenv, environ, argv, expected', [
    ({}, {}, (), False),
    ({'UNIT_COVERAGE_FULL': 'True'}, {}, (), True),
    ({'UNIT_COVERAGE_FULL': 'True'}, {'UNIT_COVERAGE_FULL': 'False'}, (), False),
    ({'UNIT_COVERAGE_FULL': 'True'}, {'UNIT_COVERAGE_FULL': 'False'}, ('--unit-coverage-full',), True),
    ({}, {'UNIT_COVERAGE_FULL': 'True'}, ('--unit-coverage-full=False',), False),
])
def test_full_setting_is_hidden_and_preserves_normal_precedence(dotenv, environ, argv, expected):
    shared = load_shared_schema()
    declaration = shared.fields['UNIT_COVERAGE_FULL']
    schema = ConfigSchema({key: shared.fields[key]
                           for key in ('UNIT_COVERAGE_FULL', 'TUI_MODE', 'COLLECT_ONLY')})

    result = resolve_config(schema, argv=argv, environ={'TUI_MODE': 'unit', **environ}, dotenv=dotenv)

    assert not result.errors
    assert result.values['UNIT_COVERAGE_FULL'] is expected
    assert declaration.default is False
    assert declaration.acknowledge_default is False
