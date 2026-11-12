"""A service held by an owner process is stopped with it, also when startup is cut short."""
import os
import signal
import time

import pytest

from api_agent.testing.owned_service import OwnedService

HOLD = '''
trap 'echo cleaned > "$1"' EXIT
printf 'ready %s\\n' "$2"
read -r _ || true
'''
HANG = '''
trap 'echo cleaned > "$1"' EXIT
touch "$1.started"
sleep 30
printf 'ready\\n'
'''


def start(script, arguments, *, seconds=30, cancelled=lambda: False):
    return OwnedService(script, arguments, environment=os.environ, deadline=time.monotonic() + seconds,
                        cancelled=cancelled)


def test_the_owner_reports_ready_and_cleans_up_when_stopped(tmp_path):
    marker = tmp_path / 'cleaned'
    service = start(HOLD, (str(marker), 'one'))

    assert service.line == 'ready one' and not marker.exists()
    assert service.stop() == 0
    assert marker.read_text() == 'cleaned\n'


def test_the_owner_cleans_up_when_the_launcher_dies(tmp_path):
    marker = tmp_path / 'cleaned'
    service = start(HOLD, (str(marker), 'one'))
    pid = service._process.pid
    service._process.stdin.close()

    deadline = time.monotonic() + 10
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    service.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_an_owner_that_exits_before_it_is_ready_reports_its_exit_code(tmp_path):
    service = start('echo failing >&2; exit 3', ())

    assert service.line == '' and service.stop() == 3


def test_startup_past_its_deadline_stops_the_owner_group():
    began = time.monotonic()

    with pytest.raises(TimeoutError):
        start(HANG, ('/dev/null',), seconds=-1)

    assert time.monotonic() - began < 10


def test_cancelled_startup_stops_the_owner_group_after_it_cleans_up(tmp_path):
    marker = tmp_path / 'cleaned'

    with pytest.raises(InterruptedError):
        start(HANG, (str(marker),), cancelled=lambda: marker.with_name('cleaned.started').exists())

    assert marker.read_text() == 'cleaned\n'


def test_an_interrupt_while_waiting_stops_the_owner_group(tmp_path, monkeypatch):
    marker = tmp_path / 'cleaned'
    def interrupt(self, timeout=None):
        if not marker.with_name('cleaned.started').exists():
            return []
        raise KeyboardInterrupt
    monkeypatch.setattr('selectors.DefaultSelector.select', interrupt)

    with pytest.raises(KeyboardInterrupt):
        start(HANG, (str(marker),))

    assert marker.read_text() == 'cleaned\n'
