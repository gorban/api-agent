"""Run publication fixtures under an independent, owned process watchdog."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import coverage
import pytest

from api_agent.providers.progress import report_timeout_sync
from api_agent.testing.processes import disposal_complete, finalize

HELPERS = Path(__file__).parent / 'helpers'
CASES = HELPERS / 'setup_publication_cases.py'


class PublicationProcessFailure(AssertionError):
    """The exact child handle and diagnostic files survive a fixture failure."""
    def __init__(self, message, process, directory):
        super().__init__(f'{message}; diagnostics: {directory / "output.txt"}')
        self.process = process
        self.directory = directory


def run_case(directory, case, *, startup_budget=60, case_budget=30, armed_budget=None, child_entrypoint=None):
    directory.mkdir()
    paths = {name: str(directory / name) for name in
             ('admitted', 'entered', 'armed', 'traced')}
    active = coverage.Coverage.current()
    settings = dict(paths, node=f'{CASES}::{case}',
                    data_file=str(directory / 'coverage'),
                    relative_files=active.config.relative_files if active else False,
                    context=active.config.context if active else '',
                    source=list(active.config.source) if active and active.config.source
                    else ['api_agent'])
    config = directory / 'settings.json'
    config.write_text(json.dumps(settings))
    environment = os.environ.copy()
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    environment.pop('PYTEST_ADDOPTS', None)
    environment['PUBLICATION_CONTROL_ARMED'] = paths['armed']
    # The child owns its own tracer and data file, never a worker's pytest-cov
    # activation or shared output file.
    for name in tuple(environment):
        if name.startswith(('COV_CORE_', 'COVERAGE_')):
            environment.pop(name)
    with (directory / 'output.txt').open('w') as output:
        process = subprocess.Popen([sys.executable, str(child_entrypoint or HELPERS / 'publication_child.py'), str(config)],
                                   stdout=output, stderr=subprocess.STDOUT, env=environment,
                                   start_new_session=True)
        try:
            with report_timeout_sync('Publication child admission', startup_budget) as admission:
                while not Path(paths['entered']).exists():
                    if process.poll() is not None:
                        raise PublicationProcessFailure('Publication child exited before case admission',
                                                        process, directory)
                    if admission.remaining <= 0:
                        raise PublicationProcessFailure('Publication child admission watchdog expired',
                                                        process, directory)
                    time.sleep(.01)
            with report_timeout_sync('Publication child execution', case_budget) as execution:
                armed_at = None
                while process.poll() is None:
                    if armed_budget is not None and Path(paths['armed']).exists() and armed_at is None:
                        armed_at = time.monotonic()
                    if (execution.remaining <= 0 or (armed_at is not None
                            and time.monotonic() - armed_at >= armed_budget)):
                        raise PublicationProcessFailure('Publication child execution watchdog expired',
                                                        process, directory)
                    time.sleep(.01)
            if process.returncode:
                raise PublicationProcessFailure('Publication child assertion failed; see output.txt',
                                                process, directory)
            assert Path(paths['traced']).read_text() == 'traced', 'Child tracing receipt missing'
            child_data = coverage.CoverageData(basename=settings['data_file'])
            child_data.read()
            assert child_data.has_arcs(), 'Child branch data missing'
            if active is not None:
                active.get_data().update(child_data)
        finally:
            finalize(process)
            assert disposal_complete(process), 'Owned publication child disposal unconfirmed'
    return SimpleNamespace(process=process, directory=directory)


@pytest.mark.parametrize('case', [
    'test_publication_stops_shielded_initialization_before_freezing_accounting',
    'test_unsettled_authentication_retains_publication_cleanup_receipt',
    'test_ready_accounting_is_finalized_before_publication',
    'test_unstarted_backend_has_no_publication_side_effects',
    'test_delayed_loader_does_not_expire_before_setup',
    'test_harness_preserves_early_transport_outcome',
    'test_harness_missing_startup_drains_request',
    'test_harness_cancellation_drains_request_without_cancelling_signal',
    'test_control_resistant_drain_receipt',
    'test_control_slow_successful_drain',
    'test_control_missing_request_start',
])
def test_publication_case_is_supervised(tmp_path, case):
    result = run_case(tmp_path / 'child', case)
    assert result.process.returncode == 0 and disposal_complete(result.process)


@pytest.mark.parametrize('case', ['test_control_blocked_loop',
                                  'test_control_permanently_resistant_request'])
def test_watchdog_reaps_actual_blocked_child_without_touching_sibling(tmp_path, case):
    sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                               start_new_session=True)
    try:
        with pytest.raises(PublicationProcessFailure, match='execution watchdog') as caught:
            run_case(tmp_path / 'child', case, armed_budget=.3)
        process = caught.value.process
        assert process is not sibling and process.pid != sibling.pid
        assert (caught.value.directory / 'armed').read_text() == 'armed'
        assert process.poll() is not None and disposal_complete(process)
        assert sibling.poll() is None
    finally:
        finalize(sibling)
        assert disposal_complete(sibling)


def test_child_assertions_cannot_be_reported_as_expected_readiness_deadline(tmp_path):
    with pytest.raises(PublicationProcessFailure, match='assertion failed') as caught:
        run_case(tmp_path / 'child', 'test_control_unrelated_failure')
    assert caught.value.process.returncode != 0
    assert disposal_complete(caught.value.process)



def test_admission_watchdog_reaps_child_that_never_enters_case(tmp_path):
    entrypoint = tmp_path / 'missing_admission.py'
    entrypoint.write_text('import time; time.sleep(120)')
    with pytest.raises(PublicationProcessFailure, match='admission watchdog') as caught:
        run_case(tmp_path / 'child', 'unused', startup_budget=.2,
                 child_entrypoint=entrypoint)
    assert caught.value.process.poll() is not None
    assert disposal_complete(caught.value.process)
    assert not (caught.value.directory / 'entered').exists()
