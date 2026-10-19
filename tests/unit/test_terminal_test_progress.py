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



def test_huge_identities_are_bounded_without_merging_distinct_completions():
    prefix = 'x' * 1_000_000
    first = SimpleNamespace(id=prefix + 'first')
    second = SimpleNamespace(id=prefix + 'second')
    lines = []
    progress = TerminalTestProgress([first, second], lines.append)
    progress(RunnerEvent('test_finished', identity=first, status='passed'))
    progress(RunnerEvent('test_finished', identity=second, status='failed'))
    progress(RunnerEvent('test_finished', identity=first, status='passed'))
    assert len(lines) == 2
    assert lines[0].startswith('Test progress: 1/2 completed — passed "')
    assert lines[1].startswith('Test progress: 2/2 completed — failed "')
    assert all(len(line) == 1024 and line.endswith(' … [truncated]') for line in lines)
    assert first.id == prefix + 'first' and second.id == prefix + 'second'


def test_registered_credential_crossing_display_limit_is_scrubbed_before_truncation():
    from api_agent.testing.diagnostics import CredentialScrubber
    secret = 'synthetic-' + 'q' * 1600 + '-suffix'
    identity = SimpleNamespace(id='base::' + secret + '::test_case')
    lines = []
    progress = TerminalTestProgress([identity], lines.append,
                                    scrub=CredentialScrubber(secret_values=(secret,)))
    progress(RunnerEvent('test_finished', identity=identity, status='passed'))
    assert lines == ['Test progress: 1/1 completed — passed "base::[REDACTED]::test_case"']
    assert identity.id == 'base::' + secret + '::test_case'


def test_exact_display_limit_is_not_truncated():
    prefix = 'Test progress: 1/1 completed — passed "'
    identity = SimpleNamespace(id='x' * (1024 - len(prefix) - 1))
    lines = []
    TerminalTestProgress([identity], lines.append)(RunnerEvent('test_finished', identity=identity, status='passed'))
    assert lines == [prefix + identity.id + '"']
    assert len(lines[0]) == 1024


def test_scrubber_expansion_is_bounded_before_writing():
    identity = SimpleNamespace(id='base::test_case')
    lines, scrubbed = [], []
    def scrub(line):
        scrubbed.append(line)
        return line + ' synthetic safe expansion' * 100
    TerminalTestProgress([identity], lines.append, scrub=scrub)(
        RunnerEvent('test_finished', identity=identity, status='passed'))
    assert scrubbed == ['Test progress: 1/1 completed — passed "base::test_case"']
    assert len(lines[0]) == 1024
    assert lines[0].endswith(' … [truncated]')
