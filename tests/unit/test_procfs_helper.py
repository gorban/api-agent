"""The shared procfs reader treats every reaping outcome as gone and nothing else."""
from pathlib import Path
import sys

import pytest

from procfs_support import is_gone_or_zombie, process_state


class _File:
    def __init__(self, read):
        self._read = read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._read()


def _opening(monkeypatch, read):
    monkeypatch.setattr(Path, 'open', lambda path, *args, **kwargs: _File(read))


def _raises(error):
    def read():
        raise error
    return read


def test_state_letter_follows_the_last_parenthesis_of_an_awkward_command_name(monkeypatch):
    _opening(monkeypatch, lambda: '77 (a (b) c) S ' + ' '.join(['1'] * 19))

    assert process_state(77) == 'S'


@pytest.mark.unit_platform('linux')
@pytest.mark.skipif(sys.platform != 'linux', reason='Linux procfs')
def test_state_reads_this_process_from_the_real_procfs():
    import os

    assert process_state(os.getpid()) in {'R', 'S'}


@pytest.mark.parametrize('error', [FileNotFoundError(2, 'gone at open'), ProcessLookupError(3, 'reaped mid-read')])
def test_reaped_task_has_no_state(monkeypatch, error):
    _opening(monkeypatch, _raises(error))

    assert process_state(4242) is None
    assert is_gone_or_zombie(4242) is True


def test_task_missing_at_open_has_no_state(monkeypatch):
    def refuse(path, *args, **kwargs):
        raise FileNotFoundError(2, 'gone at open')
    monkeypatch.setattr(Path, 'open', refuse)

    assert process_state(4242) is None


def test_permission_failures_stay_loud(monkeypatch):
    _opening(monkeypatch, _raises(PermissionError(13, 'denied')))

    with pytest.raises(PermissionError):
        process_state(4242)


@pytest.mark.parametrize('state,expected', [('Z', True), ('S', False), ('R', False)])
def test_zombies_count_as_gone_and_live_tasks_do_not(monkeypatch, state, expected):
    _opening(monkeypatch, lambda: f'9 (worker) {state} ' + ' '.join(['1'] * 19))

    assert is_gone_or_zombie(9) is expected
