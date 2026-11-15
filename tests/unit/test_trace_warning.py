import json

import pytest

from api_agent.testing.contracts import RunnerEvent
from api_agent.testing.terminal_progress import TerminalTestProgress
from api_agent.testing.trace_warning import emit_worker, message


@pytest.mark.parametrize('code', ['setup', 'delivery', 'cleanup', 'busy'])
def test_worker_emits_only_fixed_warning_code(monkeypatch, tmp_path, code):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_one (call)')
    emit_worker(code)
    emit_worker('narrative-sentinel')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [{'kind': 'trace_warning', 'node': 'test.py::test_one', 'code': code}]
    output = []
    progress = TerminalTestProgress([], output.append, allow_content=False)
    progress(RunnerEvent('trace_warning', text=code))
    progress(RunnerEvent('trace_warning', text='narrative-sentinel'))
    assert output == [message(code)]
    assert 'narrative-sentinel' not in output[0]


def test_warning_sink_failure_does_not_change_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'missing' / 'events'))
    emit_worker('setup')
