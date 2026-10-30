"""Actual pytest collection preserves admitted unit identities and ordering."""
import json
import os
import subprocess
import sys

import pytest

from api_agent.testing.runner import _PLUGIN


@pytest.fixture
def selected_unit_process(tmp_path):
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    (tmp_path / 'conftest.py').write_text(r'''
import json
from pathlib import Path
def pytest_configure(config):
    worker = getattr(config, 'workerinput', {}).get('workerid', 'main')
    Path('args-' + worker + '.json').write_text(json.dumps(config.args))
def pytest_collect_file(file_path, parent):
    worker = getattr(parent.config, 'workerinput', {}).get('workerid', 'main')
    with Path('files-' + worker + '.jsonl').open('a') as stream:
        stream.write(json.dumps(str(file_path)) + '\n')
def pytest_collection_finish(session):
    worker = getattr(session.config, 'workerinput', {}).get('workerid', 'main')
    Path('nodes-' + worker + '.json').write_text(json.dumps([item.nodeid for item in session.items]))
''')
    (tmp_path / 'test_unselected.py').write_text("raise AssertionError('Unselected module imported')\n")
    (tmp_path / 'test_cases.py').write_text('''
from pathlib import Path
import pytest
@pytest.mark.parametrize('value', ['first', 'second', 'third'])
def test_case(value):
    Path(value + '.ran').write_text(value)
def test_forbidden():
    raise AssertionError('Unselected test executed')
''')
    def run(nodes, jobs=0):
        selection = tmp_path / 'selection.json'
        selection.write_text(json.dumps(nodes))
        events = tmp_path / 'events.jsonl'
        env = dict(os.environ, API_AGENT_UNIT_SELECTION=str(selection))
        env['PYTHONPATH'] = str(tmp_path) + os.pathsep + env.get('PYTHONPATH', '')
        for key in ('API_AGENT_COVERAGE_OUTPUT', 'API_AGENT_COMPONENT_EVENTS', 'PYTEST_ADDOPTS'):
            env.pop(key, None)
        command = [sys.executable, '-c', _PLUGIN, str(events), '-q', '-o', 'addopts=']
        if jobs:
            command += ['-n', str(jobs), '--dist=worksteal', '--max-worker-restart=0']
        process = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        reports = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
        return process, reports
    return tmp_path, run


def test_serial_selection_collects_one_file_and_runs_only_ordered_unique_parameters(selected_unit_process):
    root, run = selected_unit_process
    nodes = ['test_cases.py::test_case[third]', 'test_cases.py::test_case[first]']

    process, reports = run([*nodes, nodes[0]])

    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads((root / 'args-main.json').read_text()) == [str(root)]
    assert [row['node'] for row in reports if row['kind'] == 'test_started'] == nodes
    assert {path.name for path in root.glob('*.ran')} == {'first.ran', 'third.ran'}
    assert len([row for row in reports if row.get('phase') == 'call']) == 2
    assert [json.loads(line) for line in (root / 'files-main.jsonl').read_text().splitlines()] == [str(root / 'test_cases.py')]


@pytest.mark.parametrize('jobs', [0, 2])
def test_missing_selected_identity_rejects_before_any_test_executes(selected_unit_process, jobs):
    root, run = selected_unit_process

    process, reports = run(['test_cases.py::test_case[first]', 'test_cases.py::test_case[missing]'], jobs=jobs)

    assert process.returncode != 0
    assert 'INTERNALERROR' not in process.stdout + process.stderr
    assert 'Selected unit identities were not collected' in process.stdout + process.stderr
    assert 'test_case[missing]' in process.stdout + process.stderr
    assert not reports
    assert not list(root.glob('*.ran'))


def test_parallel_workers_collect_same_exact_order_and_execute_each_selection_once(selected_unit_process):
    root, run = selected_unit_process
    nodes = ['test_cases.py::test_case[third]', 'test_cases.py::test_case[first]']

    process, reports = run([*nodes, nodes[0]], jobs=2)

    assert process.returncode == 0, process.stdout + process.stderr
    assert {path.name for path in root.glob('args-gw*.json')} == {'args-gw0.json', 'args-gw1.json'}
    assert all(json.loads(path.read_text()) == [str(root)] for path in root.glob('args-*.json'))
    assert all(json.loads(path.read_text()) == nodes for path in root.glob('nodes-gw*.json'))
    assert all([json.loads(line) for line in path.read_text().splitlines()] == [str(root / 'test_cases.py')]
               for path in root.glob('files-gw*.jsonl'))
    assert sorted(row['node'] for row in reports if row.get('phase') == 'call') == sorted(nodes)
    assert all(row['status'] == 'passed' for row in reports if row['kind'] == 'report')
    assert {path.name for path in root.glob('*.ran')} == {'first.ran', 'third.ran'}


def test_selected_files_are_unique_and_keep_cross_file_requested_order(selected_unit_process):
    root, run = selected_unit_process
    (root / 'test_other.py').write_text('def test_other():\n    pass\n')
    nodes = ['test_other.py::test_other', 'test_cases.py::test_case[third]',
             'test_cases.py::test_case[first]']

    process, reports = run(nodes)

    assert process.returncode == 0, process.stdout + process.stderr
    assert sorted(json.loads(line) for line in (root / 'files-main.jsonl').read_text().splitlines()) == [
        str(root / 'test_cases.py'), str(root / 'test_other.py')]
    assert json.loads((root / 'args-main.json').read_text()) == [str(root)]
    assert [row['node'] for row in reports if row['kind'] == 'test_started'] == nodes
    assert len([row for row in reports if row.get('phase') == 'call']) == 3


@pytest.mark.parametrize('symlink', [False, True])
def test_nested_selected_paths_preserve_alias_and_owner_conftest(selected_unit_process, symlink):
    root, run = selected_unit_process
    source = root / 'nested'
    source.mkdir()
    (source / 'conftest.py').write_text('import pytest\n@pytest.fixture\ndef token():\n    return "owner-fixture"\n')
    (source / 'test_target.py').write_text('def test_target(token):\n    assert token == "owner-fixture"\n')
    (source / 'test_unselected.py').write_text('raise AssertionError("Nested unselected import")\n')
    selected = source
    if symlink:
        selected = root / 'alias'
        selected.symlink_to(source, target_is_directory=True)
    with (root / 'conftest.py').open('a') as stream:
        stream.write('def pytest_ignore_collect(collection_path, config):\n    return True\n')
    node = selected.name + '/test_target.py::test_target'

    process, reports = run([node])

    assert process.returncode == 0, process.stdout + process.stderr
    assert [row['node'] for row in reports if row['kind'] == 'test_started'] == [node]
    assert json.loads((root / 'args-main.json').read_text()) == [str(selected)]
    assert [json.loads(line) for line in (root / 'files-main.jsonl').read_text().splitlines()] == [
        str(selected / 'test_target.py')]
