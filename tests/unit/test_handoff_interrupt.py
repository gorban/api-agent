"""Real process regression for terminal cancellation during interpreter handoff."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(os.name != 'posix', reason='Terminal process groups require POSIX')
@pytest.mark.parametrize('exit_code', [0, 1, 130])
@pytest.mark.parametrize('signal_target', ['pid', 'group'])
def test_parser_bootstrap_handoff_preserves_completed_inspection_after_signal(
        tmp_path, exit_code, signal_target):
    base = Path(__file__).resolve().parents[2]
    pack = tmp_path / 'pack'
    interpreter = pack / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    # The startup hook replaces side-effect boundaries only. The real bootstrap
    # main, exception handlers, interpreter handoff and inspection owner execute.
    (tmp_path / 'sitecustomize.py').write_text(textwrap.dedent('''
        import os
        from types import SimpleNamespace
        from api_agent.runtime import dependencies
        if os.environ.get('HANDOFF_CHILD'):
            import time
            import api_agent.config
            import api_agent.testing.collection
            import api_agent.testing.launched
            from api_agent.testing.collection import TestIdentity
            from api_agent.testing.contracts import TestResult
            from api_agent.testing.integration import IntegrationRunResult
            class Inspection:
                closed = False
                def wait_closed(self):
                    from pathlib import Path
                    ready = Path(os.environ['READY_FILE'])
                    temporary = ready.with_suffix('.pending')
                    temporary.write_text(str(os.getpid()))
                    temporary.replace(ready)
                    while True:
                        time.sleep(.01)
                def end_inspection(self):
                    time.sleep(.05)
                    self.closed = True
                    print('CLEANUP_ACKNOWLEDGED', flush=True)
                    return True
                def abort(self):
                    raise AssertionError('Acknowledged cleanup must not be aborted')
            dependencies.ensure_environment = lambda *a, **k: None
            api_agent.config.read_dotenv = lambda *a, **k: {}
            api_agent.testing.collection.collect_tests = lambda *a, **k: []
            test = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
            code = int(os.environ['SUITE_EXIT_CODE'])
            run = IntegrationRunResult(code, (TestResult(test, 'passed'),), '', code == 130, (Inspection(),))
            api_agent.testing.launched.run_launched_integration = lambda *a, **k: SimpleNamespace(
                execution=run, persisted=True, exit_code=code)
    '''))
    parent = textwrap.dedent('''
        import os, sys
        from pathlib import Path
        from types import SimpleNamespace
        from api_agent.runtime import dependencies
        def absent():
            try:
                raise ImportError('synthetic missing packaging')
            except ImportError:
                try:
                    raise ImportError('synthetic missing pip')
                except ImportError as error:
                    raise OSError('Requirement validation needs packaging or pip') from error
        dependencies._requirement_types = absent
        dependencies._prepare_requirement_parser = lambda *a: None
        base = Path(sys.argv[1])
        context = SimpleNamespace(base_root=base, pack_root=Path(sys.argv[2]),
                                  external_pack=True, show_pack_bootstrap=True)
        os.environ['HANDOFF_CHILD'] = '1'
        raise SystemExit(dependencies.ensure_environment(context, [
            '--tui-mode=integration', '--no-std-in', '--debug', '--allure-s3-bucket=synthetic'],
            replace_process=True))
    ''')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('API_AGENT', 'HANDOFF', 'TUI_', 'DEBUG', 'START', 'NO_STD_IN'))}
    environment['PYTHONPATH'] = os.pathsep.join((str(tmp_path), str(base / 'src')))
    environment['SUITE_EXIT_CODE'] = str(exit_code)
    environment['READY_FILE'] = str(tmp_path / 'ready')
    process = subprocess.Popen([sys.executable, '-c', parent, str(base), str(pack)], env=environment,
                               start_new_session=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        import time
        deadline = time.monotonic() + 10
        while not (tmp_path / 'ready').exists():
            assert process.poll() is None, process.communicate(timeout=1)
            assert time.monotonic() < deadline, 'Child never reached completed inspection'
            time.sleep(.01)
        assert (tmp_path / 'ready').read_text() == str(process.pid)
        deliver = os.kill if signal_target == 'pid' else os.killpg
        deliver(process.pid, signal.SIGINT)
        output, errors = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
    assert process.returncode == exit_code
    assert output.count('CLEANUP_ACKNOWLEDGED') == 1
    assert 'Allure publication: PASS' in output
    assert errors == ''
