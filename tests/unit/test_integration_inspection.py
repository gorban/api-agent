"""Retained worker ownership failures remain recoverable by the caller."""
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import pytest

from api_agent.testing import inspection


def test_failed_group_reap_keeps_retry_handle_and_ipc_directory(monkeypatch):
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    events = []
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    from pathlib import Path
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    event_file.write_text('{"kind":"inspection_closed"}\n')
    finalize = inspection.finalize
    attempts = []
    def transient(process):
        attempts.append(process.pid)
        if len(attempts) == 1:
            raise TimeoutError('owned descendants still pending')
        finalize(process)
    monkeypatch.setattr(inspection, 'finalize', transient)
    handle = inspection.IntegrationInspection(process, directory, event_file, reader,
                                             on_event=events.append)
    handle._monitor.join(timeout=5)
    assert handle.pending and not handle.closed
    assert event_file.exists()
    assert events[-1].kind == 'inspection_cleanup_pending'
    assert handle.end_inspection(timeout=5)
    assert handle.closed and not handle.pending
    assert not event_file.exists()
    assert events[-1].kind == 'inspection_closed'
    assert attempts == [process.pid, process.pid]


@pytest.mark.parametrize('content', [
    'invalid\n{"kind":"inspection_closed"}\n',
    '{"kind":"inspection_closed"}\n{"kind":',
    '[]\n{"kind":"inspection_closed"}\n',
])
def test_completed_acknowledgement_survives_malformed_or_truncated_neighbor(content):
    from pathlib import Path
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    event_file.write_text(content)
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    events = []
    handle = inspection.IntegrationInspection(process, directory, event_file, reader,
                                             on_event=events.append)
    try:
        handle._monitor.join(timeout=5)
        assert handle.closed
        assert [event.kind for event in events] == ['inspection_closed']
    finally:
        handle.abort()


def test_monitor_completion_callback_can_reenter_handle():
    from pathlib import Path
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    event_file.write_text('{"kind":"inspection_closed"}\n')
    marker = Path(directory.temporary.name) / 'exit'
    process = subprocess.Popen([sys.executable, '-c',
        'import pathlib, sys, time\np=pathlib.Path(sys.argv[1])\nwhile not p.exists(): time.sleep(.01)',
        str(marker)], stdout=subprocess.PIPE, text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    completed = threading.Event()
    def event(row):
        assert handle.end_inspection(timeout=.1)
        handle.abort()
        completed.set()
    handle = inspection.IntegrationInspection(process, directory, event_file, reader, on_event=event)
    marker.touch()
    try:
        assert completed.wait(timeout=5)
        handle._monitor.join(timeout=1)
        assert not handle._monitor.is_alive()
    finally:
        handle.abort()


def test_reader_still_running_keeps_pending_ownership_for_retry(monkeypatch):
    from pathlib import Path
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    event_file.write_text('{"kind":"inspection_closed"}\n')
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    process.wait(timeout=5)
    gate = threading.Event()
    reader = threading.Thread(target=gate.wait, daemon=True)
    reader.start()
    bound_reader = SimpleNamespace(join=lambda timeout: reader.join(timeout=.01),
                                   is_alive=reader.is_alive)
    original_start = threading.Thread.start
    def start(thread):
        if getattr(thread._target, '__name__', '') != '_watch':
            original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    events = []
    handle = inspection.IntegrationInspection(process, directory, event_file, bound_reader,
                                             on_event=events.append)
    try:
        assert not handle.end_inspection(timeout=1)
        assert handle.pending and not handle.closed
        assert event_file.exists() and not process.stdout.closed
        assert events[-1].kind == 'inspection_cleanup_pending'
        gate.set()
        reader.join(timeout=1)
        assert handle.end_inspection(timeout=1)
        assert handle.closed and not handle.pending
        assert not event_file.exists() and process.stdout.closed
    finally:
        gate.set()
        reader.join(timeout=1)
        handle.abort()


def test_unresponsive_worker_timeout_retains_retry_handle():
    from pathlib import Path
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    event_file.write_text('')
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    handle = inspection.IntegrationInspection(process, directory, event_file, reader)
    try:
        assert not handle.end_inspection(timeout=0)
        assert handle.pending and not handle.closed and event_file.exists()
        assert not handle.wait_closed(timeout=.01)
        handle.abort()
        assert handle.wait_closed(timeout=1)
        handle._monitor.join(timeout=1)
        assert not handle._monitor.is_alive()
    finally:
        handle.abort()


@pytest.mark.parametrize('broken', ['missing', 'encoding'])
def test_unreadable_protocol_and_failing_callback_still_reap_worker(broken):
    from pathlib import Path
    directory = inspection.InspectionDirectory(tempfile.TemporaryDirectory())
    event_file = Path(directory.temporary.name) / 'events.jsonl'
    if broken == 'encoding':
        event_file.write_bytes(b'\xff\n')
    process = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    reader = threading.Thread(target=lambda: list(process.stdout), daemon=True)
    reader.start()
    events = []
    def callback(event):
        events.append(event)
        raise RuntimeError('private callback detail')
    handle = inspection.IntegrationInspection(process, directory, event_file, reader,
                                             on_event=callback)
    try:
        handle._monitor.join(timeout=5)
        assert handle.closed and not handle.pending
        assert [event.kind for event in events] == ['inspection_lost']
    finally:
        handle.abort()
