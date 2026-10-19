"""Execution/collection boundary failures with controlled owned-process adapters."""
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from api_agent.testing import collection, runner, TestIdentity, SelectionModel, CollectionError
from api_agent.testing import processes


def identity(repo='base', name='test_one'):
    return TestIdentity(f'{repo}::tests/unit/test.py::{name}', repo, 'tests/unit/test.py', f'tests/unit/test.py::{name}')


def context(tmp_path, *, source=True):
    base = tmp_path / 'base'
    base.mkdir()
    (base / 'tests/unit').mkdir(parents=True)
    if source:
        (base / 'src').mkdir()
        (base / 'src/code.py').write_text('VALUE = 1\n')
    return SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [base / 'tests/unit'])


@pytest.mark.parametrize('module', [collection, runner])
def test_nonposix_process_teardown_escalates_and_reaps(monkeypatch, module):
    process = Mock()
    process.communicate.side_effect = [subprocess.TimeoutExpired('pytest', .5), ('', '')]
    process.wait.side_effect = [subprocess.TimeoutExpired('pytest', .5), 0]
    monkeypatch.setattr(processes, 'os', SimpleNamespace(name='nt'))
    (module._stop_collection if module is collection else module._stop_execution)(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert (process.communicate if module is collection else process.wait).call_count == 2


def test_nonposix_teardown_does_not_require_sigkill(monkeypatch):
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired('pytest', .5), 0]
    monkeypatch.setattr(processes, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(processes, 'signal', SimpleNamespace(SIGTERM=15))
    processes.finalize(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


@pytest.mark.parametrize('reaped', [False, True])
def test_nonposix_interrupted_disposal_requires_confirmed_leader(monkeypatch, reaped):
    interruption = KeyboardInterrupt('controlled interrupt')
    process = SimpleNamespace(returncode=0 if reaped else None, terminate=Mock(),
                              wait=Mock(side_effect=interruption))
    monkeypatch.setattr(processes, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(KeyboardInterrupt) as caught:
        processes.finalize(process)
    assert caught.value is interruption
    assert processes.disposal_complete(process) is reaped
    process.terminate.assert_called_once()


@pytest.mark.parametrize('listing,running', [
    (' 123 Z\n 456 S\n', False),
    (' 123 S\n 456 Z\n', True),
    ('', False),
])
def test_group_detection_without_proc_ignores_zombies(monkeypatch, listing, running):
    monkeypatch.setattr(processes, 'Path', lambda path: SimpleNamespace(is_dir=lambda: False))
    calls = []
    def ps(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=listing)
    monkeypatch.setattr(processes.subprocess, 'run', ps)
    assert processes._group_running(123) is running
    assert calls == [((['ps', '-A', '-o', 'pgid=', '-o', 'state='],),
                      {'capture_output': True, 'text': True, 'check': True, 'timeout': .5})]


@pytest.mark.parametrize('listing', ['bad row', '123', 'abc S'])
def test_group_detection_rejects_malformed_ps(monkeypatch, listing):
    monkeypatch.setattr(processes, 'Path', lambda path: SimpleNamespace(is_dir=lambda: False))
    monkeypatch.setattr(processes.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=listing))
    with pytest.raises(RuntimeError, match='invalid ps output'):
        processes._group_running(123)


@pytest.mark.parametrize('failure', [OSError('missing ps'), subprocess.CalledProcessError(1, 'ps'),
                                      subprocess.TimeoutExpired('ps', .5)])
def test_group_detection_rejects_ps_failure(monkeypatch, failure):
    monkeypatch.setattr(processes, 'Path', lambda path: SimpleNamespace(is_dir=lambda: False))
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(processes.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='ps failed'):
        processes._group_running(123)


def test_group_detection_and_wait_for_live_descendant(monkeypatch, tmp_path):
    process = tmp_path / '123' / 'stat'
    process.parent.mkdir()
    process.write_text('123 (pytest child) S 1 123 123 0\n')
    real_path = Path
    monkeypatch.setattr(processes, 'Path', lambda path: real_path(tmp_path) if path == '/proc' else real_path(path))
    assert processes._group_running(123)
    process.write_text('123 (pytest child) Z 1 123 123 0\n')
    assert not processes._group_running(123)
    alive = iter([True, False])
    monkeypatch.setattr(processes, '_group_running', lambda group: next(alive))
    monkeypatch.setattr(processes.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(processes.os, 'killpg', lambda *args: None)
    fake = SimpleNamespace(pid=123, wait=lambda **kwargs: 0)
    processes.finalize(fake)


def test_finalization_reports_unstopped_process_group(monkeypatch):
    monkeypatch.setattr(processes.os, 'killpg', lambda *args: None)
    monkeypatch.setattr(processes, '_group_running', lambda group: True)
    times = iter([0, 2])
    monkeypatch.setattr(processes.time, 'monotonic', lambda: next(times))
    fake = SimpleNamespace(pid=123, wait=lambda **kwargs: 0)
    with pytest.raises(TimeoutError, match='did not stop after SIGKILL'):
        processes.finalize(fake)


def test_collection_start_failure_is_actionable_and_does_not_stop_unowned_process(monkeypatch, tmp_path):
    monkeypatch.setattr(collection.subprocess, 'Popen', Mock(side_effect=OSError('missing python')))
    with pytest.raises(CollectionError, match='Cannot start pytest collection for base'):
        collection._run_collection([], tmp_path, {}, 'base', None)


@pytest.mark.parametrize('payload,mode,message', [
    ('[]', 'unit', 'No tests collected in base unit suite'),
    ('{}', 'integration', 'No tests collected for integration'),
    ('[{"bad":1}]', 'unit', 'invalid collection data'),
    ('invalid json', 'unit', 'invalid collection data'),
])
def test_collection_rejects_empty_or_invalid_identity_data(monkeypatch, tmp_path, payload, mode, message):
    ctx = context(tmp_path)
    def collect(args, *unused):
        Path(args[3]).write_text(payload)
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(collection, '_run_collection', collect)
    with pytest.raises(CollectionError, match=message):
        collection.collect_tests(ctx, mode)


def test_collection_rejects_external_suite_and_absent_declarations(tmp_path):
    ctx = context(tmp_path)
    outside = tmp_path / 'outside'; outside.mkdir()
    ctx.suite_paths = lambda mode: [outside]
    with pytest.raises(CollectionError, match='outside its repository owner'):
        collection.collect_tests(ctx, 'unit')
    ctx.suite_paths = lambda mode: []
    with pytest.raises(CollectionError, match='No unit suites are declared'):
        collection.collect_tests(ctx, 'unit')


def test_matching_owner_names_and_inherited_pythonpath_are_preserved(monkeypatch, tmp_path):
    ctx = context(tmp_path)
    pack = tmp_path / 'pack-parent/base'; (pack / 'tests/unit').mkdir(parents=True)
    ctx.owner_root = pack
    ctx.suite_paths = lambda mode: [ctx.base_root / 'tests/unit', pack / 'tests/unit']
    seen = []
    monkeypatch.setenv('PYTHONPATH', '/extra/path')
    def collect(args, root, env, repo, cancel_event):
        seen.append((repo, env['PYTHONPATH']))
        Path(args[3]).write_text(json.dumps([{'node_id':'tests/unit/test.py::test_one', 'tags':[]}]))
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(collection, '_run_collection', collect)
    assert {test.repo for test in collection.collect_tests(ctx, 'unit')} == {'base', 'pack'}
    assert [repo for repo, _ in seen] == ['base', 'pack']
    assert all(path.endswith('/extra/path') for _, path in seen)


def test_preselected_tag_and_all_scope(tmp_path):
    a = TestIdentity('base::a.py::a', 'base', 'a.py', 'a.py::a', {'mutation'})
    b = TestIdentity('base::b.py::b', 'base', 'b.py', 'b.py::b')
    selection_state = SelectionModel([a,b]); selection_state.preselect(['tag:mutation'])
    assert selection_state.selected_scopes == {'tag:mutation'}
    selection_state.preselect(['a.py','b.py'])
    assert selection_state.selected_scopes == {'file:base::a.py', 'file:base::b.py'}


def fake_execution(monkeypatch, ctx, *, reports=(), coverage='valid', exit_code=0, output='pytest output\n'):
    test = identity(ctx.base_root.name)
    monkeypatch.setattr(runner, 'collect_tests', lambda *args, **kwargs: [test])
    stops = []
    monkeypatch.setattr(runner, '_stop_execution', lambda process: stops.append(process))
    def popen(args, **kwargs):
        Path(args[3]).write_text(''.join(json.dumps(row)+'\n' for row in reports))
        destination = kwargs['env'].get('API_AGENT_COVERAGE_OUTPUT')
        if destination and coverage != 'missing':
            document = {'files': {'src/code.py': {'summary': {'covered_lines':1, 'num_statements':1, 'covered_branches':2, 'num_branches':2}}}}
            if coverage == 'empty': document = {'files': {}}
            if coverage == 'branch-gap': document['files']['src/code.py']['summary']['covered_branches'] = 1
            Path(destination).write_text(json.dumps(document))
            Path(kwargs['env']['COVERAGE_FILE']).touch()
            html = Path(kwargs['env']['API_AGENT_COVERAGE_HTML'])
            html.mkdir()
            (html / 'index.html').write_text('Synthetic coverage report')
        return SimpleNamespace(stdout=io.StringIO(output), returncode=exit_code, poll=lambda: exit_code)
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    return test, stops


def report(phase='call', status='passed', node=None):
    return {'kind':'report', 'node':node or identity().node_id, 'phase':phase, 'status':status, 'duration':.1, 'traceback':'failure details' if status=='failed' else ''}


@pytest.mark.parametrize('reports,status', [
    ([report('setup','failed'),report('teardown')], 'failed'),
    ([report('setup','skipped'),report('teardown')], 'skipped'),
    ([report('setup'),report('teardown')], 'error'),
    ([report()], 'error'),
    ([], 'error'),
])
def test_runner_accounts_for_setup_skip_missing_call_and_missing_teardown(monkeypatch, tmp_path, reports, status):
    ctx=context(tmp_path); test,stops=fake_execution(monkeypatch,ctx,reports=reports)
    result=runner.run_unit_tests(ctx,[test])
    assert result.tests[0].status == status
    assert result.exit_code == int(status != 'passed')
    assert stops and stops[0].stdout.closed


@pytest.mark.parametrize('coverage', ['empty','missing'])
def test_runner_cannot_pass_without_valid_coverage(monkeypatch,tmp_path,coverage):
    ctx=context(tmp_path); test,_=fake_execution(monkeypatch,ctx,reports=[report('setup'), report(), report('teardown')],coverage=coverage)
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code == 1
    assert not result.coverage
    if coverage=='missing': assert 'Coverage report unavailable' in result.output


def test_runner_unknown_events_are_ignored_and_no_production_source_is_supported(monkeypatch,tmp_path):
    ctx=context(tmp_path,source=False)
    test,_=fake_execution(monkeypatch,ctx,reports=[report(node='unselected'), report('setup'), report(), report('teardown')])
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert not result.coverage


def test_runner_rejects_bad_parallelism_and_fabricated_identity(monkeypatch,tmp_path):
    ctx=context(tmp_path); test,_=fake_execution(monkeypatch,ctx)
    with pytest.raises(ValueError,match='nonnegative'):
        runner.run_unit_tests(ctx,[test],parallel_jobs=-1)
    with pytest.raises(CollectionError,match='outside the collected suites'):
        runner.run_unit_tests(ctx,[identity(name='fake')])


def test_runner_cannot_start_pytest_returns_error_result(monkeypatch,tmp_path):
    ctx=context(tmp_path); test,_=fake_execution(monkeypatch,ctx)
    monkeypatch.setattr(runner.subprocess,'Popen',Mock(side_effect=OSError('missing executable')))
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'Cannot start pytest' in result.output


def test_runner_branch_gap_is_fatal_only_for_full_coverage(monkeypatch,tmp_path):
    ctx=context(tmp_path); test,_=fake_execution(monkeypatch,ctx,reports=[report('setup'), report(), report('teardown')],coverage='branch-gap')
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code == 1 and result.tests[0].status == 'passed'
    assert result.coverage[0].line_percent == 100 and result.coverage[0].branch_percent == 50
    assert 'Coverage FAIL' in runner.format_unit_summary(result)
    assert runner.run_unit_tests(ctx,[test],require_full_coverage=False).exit_code == 0


def test_cancellation_between_discovery_and_execution_starts_no_process(monkeypatch,tmp_path):
    import threading
    ctx=context(tmp_path); test,_=fake_execution(monkeypatch,ctx)
    cancel=threading.Event()
    def collect(*args,**kwargs):
        cancel.set()
        return [test]
    monkeypatch.setattr(runner,'collect_tests',collect)
    launch=Mock(); monkeypatch.setattr(runner.subprocess,'Popen',launch)
    result=runner.run_unit_tests(ctx,[test],cancel_event=cancel)
    assert result.cancelled and result.exit_code == 130
    assert result.tests[0].status == 'cancelled'
    launch.assert_not_called()


def test_runner_identically_named_repositories_remain_distinct(monkeypatch,tmp_path):
    ctx=context(tmp_path); pack=tmp_path/'second/base';pack.mkdir(parents=True)
    ctx.owner_root=pack
    base_test,stops=fake_execution(monkeypatch,ctx,reports=[report('setup'), report(), report('teardown')])
    pack_test=identity('pack')
    monkeypatch.setattr(runner,'collect_tests',lambda *args,**kwargs:[base_test,pack_test])
    result=runner.run_unit_tests(ctx,[base_test,pack_test])
    assert result.exit_code == 0
    assert [test.identity.repo for test in result.tests] == ['base','pack']
    assert len(stops)==2


def test_invalid_runner_event_reaps_process_before_propagating(monkeypatch,tmp_path):
    ctx=context(tmp_path); test,stops=fake_execution(monkeypatch,ctx,reports=[{'unexpected':'invalid'}])
    with pytest.raises(KeyError):
        runner.run_unit_tests(ctx,[test])
    assert len(stops)==1 and stops[0].stdout.closed


def test_still_active_output_reader_retains_stream_ownership(monkeypatch,tmp_path):
    ctx=context(tmp_path); test,stops=fake_execution(monkeypatch,ctx,reports=[report('setup'), report(), report('teardown')])
    class Reader:
        def __init__(self,**kwargs):pass
        def start(self):pass
        def join(self,timeout):assert timeout==1
        def is_alive(self):return True
    monkeypatch.setattr(runner,'threading',SimpleNamespace(Thread=Reader))
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code == 0
    assert not stops[0].stdout.closed  # It belongs to the still-draining reader.
    stops[0].stdout.close()


@pytest.mark.parametrize('failure',[False,True])
def test_unselected_production_owner_report_is_measured_or_fails_explicitly(monkeypatch,tmp_path,failure):
    import coverage
    ctx=context(tmp_path);pack=tmp_path/'pack';(pack/'src').mkdir(parents=True)
    (pack/'src/uncovered.py').write_text('VALUE = 1\n');ctx.owner_root=pack
    test,_=fake_execution(monkeypatch,ctx,reports=[report('setup'), report(), report('teardown')])
    calls=[]
    class Coverage:
        def __init__(self,**kwargs):
            assert kwargs['source']==[str(pack/'src')]
            self.data_file = Path(kwargs['data_file'])
        def set_option(self,*args):calls.append('configure')
        def start(self):calls.append('start')
        def stop(self):calls.append('stop')
        def save(self):
            self.data_file.write_bytes(b'synthetic coverage database')
        def html_report(self,*,morfs,omit,directory):
            assert morfs == [str(pack/'src/uncovered.py')]
            assert omit == ['*/tests/*']
            Path(directory).mkdir()
            (Path(directory)/'index.html').write_text('Synthetic uncovered owner')
        def json_report(self,*,morfs,outfile,omit):
            assert morfs==[str(pack/'src/uncovered.py')]
            assert omit == ['*/tests/*']
            if failure:raise OSError('report unavailable')
            Path(outfile).write_text(json.dumps({'files':{'src/uncovered.py':{'summary':{'covered_lines':0,'num_statements':1,'covered_branches':0,'num_branches':2}}}}))
    monkeypatch.setattr(coverage,'Coverage',Coverage)
    result=runner.run_unit_tests(ctx,[test])
    assert result.exit_code==1 and result.tests[0].status=='passed'
    assert calls==['configure','start','stop']
    if failure:assert 'Coverage report unavailable for unselected owner' in result.output
    else:assert any(area.repo=='pack' and area.covered_lines==0 for area in result.coverage)


def test_no_production_files_summary_is_explicit():
    result=runner.UnitRunResult(0,(runner.TestResult(identity(),'passed'),),(),'')
    assert 'base: no measured code files' in runner.format_unit_summary(result)
