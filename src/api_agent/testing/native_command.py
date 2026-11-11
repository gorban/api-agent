"""Finite native CLI supervisor; private intents are never diagnostic attachments.

It owns the actual command's Popen reservation independently of its pytest worker.
Only confirmed canonical process-group disposal permits a completion receipt.
"""
import math
import os
from pathlib import Path
import signal
import sys
from threading import Event

from api_agent.runtime.prerequisites import run_command
from .native_recovery import _read, _request_digest, _write


def main():
    directory, descriptor = Path(sys.argv[1]), int(sys.argv[2])
    request = _read(directory / 'request.json')
    if (type(request) is not dict or set(request) != {'version', 'command', 'environment',
            'directory', 'deadline', 'combine_output', 'output_limit'}
            or type(request['version']) is not int or request['version'] != 1
            or type(request['command']) is not list or not request['command']
            or any(type(item) is not str for item in request['command'])
            or type(request['environment']) is not dict
            or any(type(key) is not str or type(value) is not str
                for key, value in request['environment'].items())
            or type(request['directory']) is not str or not Path(request['directory']).is_absolute()
            or type(request['deadline']) not in (int, float) or not math.isfinite(request['deadline'])
            or type(request['combine_output']) is not bool
            or (request['output_limit'] is not None and (type(request['output_limit']) is not int
                or request['output_limit'] < 256))):
        raise ValueError('Invalid private native command intent')
    os.fstat(descriptor)
    cancelled = Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    output, error = '', None
    confirmed = False
    def disposed():
        nonlocal confirmed
        confirmed = True
    try:
        try:
            output = run_command(tuple(request['command']), environment=request['environment'],
                directory=request['directory'], deadline=request['deadline'], cancelled=cancelled.is_set,
                combine_output=request['combine_output'], output_limit=request['output_limit'],
                inherited_fds=(descriptor,), on_disposed=disposed, on_not_started=disposed)
        except BaseException as failure:
            error = failure
            if not confirmed:
                # A killed/failed supervisor with no receipt remains pending.
                raise
        if not confirmed:
            raise ValueError('Native command disposal was not confirmed')
        _write(directory / 'complete.json', {'version': 1,
            'request_sha256': _request_digest(request), 'disposed': True})
    finally:
        signal.signal(signal.SIGTERM, previous)
    if error is not None:
        print(getattr(error, 'local_details', 'Native command failed after confirmed disposal.'), file=sys.stderr)
        raise SystemExit(1)
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except BrokenPipeError:
        # Worker death does not invalidate already confirmed native disposal.
        pass
