"""Terminal status never depends on buffered pytest stdout."""
from types import SimpleNamespace

import pytest

from api_agent.testing.contracts import RunnerEvent
from api_agent.testing.terminal_progress import TerminalTestProgress


def test_terminal_progress_uses_only_selected_unique_completions():
    identity = SimpleNamespace(id='base::test\n\x1b.py::test_case')
    lines = []
    progress = TerminalTestProgress([identity], lines.append)
    for event in [RunnerEvent('output', text='secret'),
                  RunnerEvent('test_started', identity=identity),
                  RunnerEvent('output', identity=identity, text='secret'), RunnerEvent('test_finished'),
                  RunnerEvent('test_finished', identity=SimpleNamespace(id='unselected')),
                  RunnerEvent('test_finished', identity=identity, status='passed', traceback='secret'),
                  RunnerEvent('test_finished', identity=identity, status='failed')]:
        progress(event)
    assert lines == ['Test progress: 1/1 completed — passed "base::test\\n\\u001b.py::test_case"']


def test_terminal_progress_restricts_status_to_fixed_vocabulary():
    identity = SimpleNamespace(id='base::test_case')
    lines = []
    TerminalTestProgress([identity], lines.append)(RunnerEvent('test_finished', identity=identity, status='secret\n'))
    assert lines == ['Test progress: 1/1 completed — error "base::test_case"']


@pytest.mark.parametrize('status', ['passed', 'failed', 'skipped', 'error', 'cancelled'])
def test_terminal_progress_counts_each_completed_outcome(status):
    identity = SimpleNamespace(id='base::test_case')
    lines = []
    TerminalTestProgress([identity], lines.append)(RunnerEvent('test_finished', identity=identity, status=status))
    assert lines == [f'Test progress: 1/1 completed — {status} "base::test_case"']
